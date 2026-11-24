"""The console's HTTP app.

GET  /               the page (static HTML, CSS and JS, no build step)
GET  /login          start sign-in: Keycloak, authorization code + PKCE
GET  /callback       finish sign-in; tokens stay on the server
POST /logout         end the session here and at Keycloak
GET  /api/session    who is signed in, what's left of the limits, the turns so far
POST /api/chat       one turn of the agent, with everything that happened behind it
POST /api/reset      start the conversation again
GET  /metrics        Prometheus (the edge doesn't expose it publicly)
GET  /healthz
"""

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from importlib import resources
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from crewgate.agent import Agent, GatewayTools
from crewgate.agent.graph import TurnResult
from crewgate.agent.llm import ChatModel, LLMError, LLMQuotaExceeded
from crewgate.agent.reply_guard import Facts

from .metrics import AgentMetrics
from .oidc import Oidc, SignInError
from .sessions import Session, SessionStore
from .settings import WebSettings

log = logging.getLogger("crewgate.web")
COOKIE = "crewgate_session"
_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}
_STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/app.css": ("app.css", "text/css"),
}


class DailyBudget:
    """Model calls left today (UTC) across all users: a public demo can't drain the free quota."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.time) -> None:
        self.limit, self.clock = limit, clock
        self.day, self.used = "", 0

    def _today(self) -> None:
        day = dt.datetime.fromtimestamp(self.clock(), dt.UTC).date().isoformat()
        if day != self.day:
            self.day, self.used = day, 0

    @property
    def left(self) -> int:
        self._today()
        return max(0, self.limit - self.used)

    def spend(self, n: int) -> None:
        self._today()
        self.used += n


def create_app(
    settings: WebSettings,
    model: ChatModel | None,
    *,
    idp_http: httpx.AsyncClient | None = None,
    gateway_app: Any = None,
    clock: Callable[[], float] = time.time,
) -> Starlette:
    """`gateway_app` runs the gateway in-process (tests); otherwise it is reached at gateway_mcp_url.
    Without a model (no API key) the console still signs people in and says chat is off."""
    s = settings
    http = idp_http or httpx.AsyncClient(timeout=15.0)
    oidc = Oidc(s, http, clock)
    store = SessionStore(s.session_ttl_seconds, clock)
    budget = DailyBudget(s.max_model_calls_per_day, clock)
    metrics = AgentMetrics()
    locks: dict[str, asyncio.Lock] = {}

    def session_of(request: Request) -> Session | None:
        return store.get(request.cookies.get(COOKIE))

    def with_cookie(resp: Response, sess: Session) -> Response:
        resp.set_cookie(
            COOKIE,
            sess.id,
            httponly=True,
            secure=s.secure_cookies,
            samesite="lax",
            max_age=s.session_ttl_seconds,
        )
        return resp

    def same_origin(request: Request) -> bool:
        """CSRF check for every POST. The custom header can't be sent cross-site without a CORS
        preflight, which this app never allows. On top of that, the browser's Sec-Fetch-Site must
        say same-origin. Proxies can rewrite Origin (GitHub Codespaces' port forwarding does), but
        Sec-Fetch-Site is set by the browser alone and scripts can't fake it. Clients that don't
        send it (scripts, old browsers) are checked on Origin instead."""
        if request.headers.get("x-crewgate") != "1":
            return False
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site in ("same-origin", "none")
        return request.headers.get("origin") == s.origin

    def turns_left(sess: Session) -> int:
        recent = [t for t in sess.turns if clock() - t < 3600]
        sess.turns = recent
        return max(0, s.max_turns_per_hour - len(recent))

    async def page(request: Request) -> Response:
        name, kind = _STATIC[request.url.path]
        body = resources.files("crewgate.web").joinpath("static", name).read_bytes()
        return Response(body, media_type=kind, headers=_HEADERS)

    async def login(request: Request) -> Response:
        sess = session_of(request) or store.new()
        url, sess.login = oidc.start()
        return with_cookie(RedirectResponse(url, 303, headers=_HEADERS), sess)

    async def callback(request: Request) -> Response:
        sess = session_of(request)
        if sess is None:
            return RedirectResponse("/?error=session_expired", 303, headers=_HEADERS)
        if request.query_params.get("error"):
            return RedirectResponse("/?error=sign_in_cancelled", 303, headers=_HEADERS)
        try:
            sess.tokens = await oidc.finish(
                sess.login, request.query_params.get("state"), request.query_params.get("code")
            )
        except SignInError as exc:
            log.warning("sign_in_failed", extra={"reason": str(exc)})
            return RedirectResponse("/?error=sign_in_failed", 303, headers=_HEADERS)
        sess.login = None
        metrics.sessions.set(len(store))
        return RedirectResponse("/", 303, headers=_HEADERS)

    async def logout(request: Request) -> Response:
        if not same_origin(request):
            return JSONResponse({"error": "bad_origin"}, 403, headers=_HEADERS)
        store.drop(request.cookies.get(COOKIE))
        metrics.sessions.set(len(store))
        params = {"client_id": s.client_id, "post_logout_redirect_uri": s.public_url.rstrip("/") + "/"}
        resp = JSONResponse({"logout_url": f"{s.logout_url}?{urlencode(params)}"}, headers=_HEADERS)
        resp.delete_cookie(COOKIE)
        return resp

    async def session_info(request: Request) -> Response:
        sess = session_of(request)
        signed_in = bool(sess and sess.tokens)
        body: dict[str, Any] = {
            "signed_in": signed_in,
            "note": s.demo_users_note,
            "grafana_url": s.grafana_url or None,
            "model_calls_left_today": budget.left,
            "chat_enabled": model is not None,
        }
        if sess and sess.tokens:
            body.update(username=sess.tokens.username, turns_left=turns_left(sess), log=sess.log)
        return JSONResponse(body, headers=_HEADERS)

    async def reset(request: Request) -> Response:
        sess = session_of(request)
        if not same_origin(request) or sess is None or sess.tokens is None:
            return JSONResponse({"error": "not_signed_in"}, 401, headers=_HEADERS)
        sess.history, sess.log, sess.facts = [], [], Facts({}, {})
        return JSONResponse({"ok": True}, headers=_HEADERS)

    async def chat(request: Request) -> Response:
        if not same_origin(request):
            return JSONResponse({"error": "bad_origin"}, 403, headers=_HEADERS)
        sess = session_of(request)
        if sess is None or sess.tokens is None:
            return JSONResponse({"error": "not_signed_in"}, 401, headers=_HEADERS)
        if model is None:
            return JSONResponse(
                {"error": "model_not_configured", "message": "Chat is off: this server has no model key."},
                503,
                headers=_HEADERS,
            )
        try:
            text = str((await request.json()).get("message") or "").strip()
        except ValueError:
            text = ""
        if not text or len(text) > s.max_message_chars:
            return JSONResponse(
                {"error": "bad_message", "max_chars": s.max_message_chars}, 400, headers=_HEADERS
            )
        if turns_left(sess) == 0:
            return JSONResponse(
                {"error": "too_many_turns", "message": "Hourly limit reached."}, 429, headers=_HEADERS
            )
        if budget.left == 0:
            return JSONResponse(
                {"error": "daily_budget", "message": "Today's model budget for this demo is used up."},
                429,
                headers=_HEADERS,
            )
        lock = locks.setdefault(sess.id, asyncio.Lock())
        async with lock:
            try:
                if oidc.needs_refresh(sess.tokens):
                    sess.tokens = await oidc.refresh(sess.tokens)
            except SignInError:
                sess.tokens = None
                return JSONResponse({"error": "not_signed_in"}, 401, headers=_HEADERS)
            sess.turns.append(clock())
            started = time.perf_counter()
            try:
                turn = await run_turn(sess, text)
            except LLMQuotaExceeded as exc:
                metrics.turns.labels("model_quota").inc()
                minutes = max(1, round(exc.retry_after / 60))
                return JSONResponse(
                    {
                        "error": "model_busy",
                        "message": f"The free model quota is used up. Try again in about {minutes} min.",
                        "retry_after": round(exc.retry_after),
                    },
                    503,
                    headers=_HEADERS,
                )
            except LLMError as exc:
                metrics.turns.labels("model_error").inc()
                log.warning("model_error", extra={"error": str(exc)})
                return JSONResponse(
                    {"error": "model_error", "message": "The model failed."}, 502, headers=_HEADERS
                )
            except Exception:  # never a stack trace to the browser
                metrics.turns.labels("internal_error").inc()
                log.exception("turn_failed")
                return JSONResponse(
                    {"error": "internal_error", "message": "Something went wrong."}, 500, headers=_HEADERS
                )
            seconds = time.perf_counter() - started
            budget.spend(sum(1 for t in turn.trace if t["kind"] == "llm"))
            metrics.record(turn, seconds)
            entry = render(text, turn, sess, seconds)
            sess.log.append(entry)
            return JSONResponse(
                {**entry, "turns_left": turns_left(sess), "model_calls_left_today": budget.left},
                headers=_HEADERS,
            )

    async def run_turn(sess: Session, text: str) -> TurnResult:
        assert sess.tokens is not None and model is not None  # noqa: S101  (checked by the caller)
        failure: LLMError | None = None
        async with GatewayTools.connect(s.gateway_mcp_url, sess.tokens.access, asgi_app=gateway_app) as tools:
            agent = Agent(model, tools, history=sess.history, facts=sess.facts)
            try:
                turn = await agent.ask(text)
            except LLMError as exc:
                failure = exc  # raised after the MCP session closes, so it isn't wrapped in a task group
            else:
                sess.history, sess.facts = agent.history, agent.facts
                return turn
        raise failure

    async def prometheus(request: Request) -> Response:
        return Response(metrics.exposition(), media_type="text/plain; version=0.0.4")

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/", page, methods=["GET"]),
            Route("/app.js", page, methods=["GET"]),
            Route("/app.css", page, methods=["GET"]),
            Route("/login", login, methods=["GET"]),
            Route("/callback", callback, methods=["GET"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/api/session", session_info, methods=["GET"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/reset", reset, methods=["POST"]),
            Route("/metrics", prometheus, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )
    app.state.store, app.state.budget, app.state.metrics = store, budget, metrics
    return app


def render(text: str, turn: TurnResult, sess: Session, seconds: float) -> dict[str, Any]:
    """What the page shows for one turn: the reply and every step behind it."""
    steps = []
    for t in turn.trace:
        if t["kind"] == "llm":
            steps.append(
                {
                    "kind": "model",
                    "ms": t["ms"],
                    "tokens": t.get("prompt_tokens", 0) + t.get("completion_tokens", 0),
                    "wants": t.get("tool_calls", []),
                    "retried": bool(t.get("retried_bad_tool_call")),
                    "model": t.get("model"),
                }
            )
        elif t["kind"] in ("tool", "blocked"):
            steps.append(
                {
                    "kind": t["kind"],
                    "tool": t["name"],
                    "arguments": t.get("arguments"),
                    "error": t.get("error"),
                    "ms": t["ms"],
                }
            )
        elif t["kind"] == "rewrite":
            steps.append({"kind": "rewrite", "to": t.get("to"), "ok": t.get("ok")})
    pending = [
        {"id": aid, "summary": a.get("summary"), "approve_url": a.get("approve_url"), "tool": a.get("tool")}
        for aid, a in sess.facts.approvals.items()
        if a.get("status") == "pending"
    ]
    bookings = [
        {
            "id": bid,
            "state": b.get("state"),
            "confirmed": b.get("confirmed"),
            "reference": b.get("booking_reference"),
        }
        for bid, b in sess.facts.bookings.items()
    ]
    return {
        "user": text,
        "reply": turn.reply,
        "guard": {
            "ok": turn.guard.ok,
            "reason": turn.guard.reason,
            "draft": None if turn.guard.ok else turn.guard.draft,
        },
        "steps": steps,
        "approvals": pending,
        "bookings": bookings,
        "tokens": turn.tokens,
        "seconds": round(seconds, 2),
    }
