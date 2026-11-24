"""The console end to end in one process: real sign-in flow against the fake IdP, the real
gateway, the agent with a scripted model, the page and its API."""

import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from crewgate.agent.llm import Completion, LLMError, LLMQuotaExceeded
from crewgate.web import WebSettings, create_app
from tests.helpers.idp import ISSUER
from tests.helpers.scripted_llm import ScriptedChat, call, last_tool, say
from tests.helpers.stack import ALL_SCOPES, MCP_URL, Stack, gateway_stack

ORIGIN = "https://console.test"
DAY = (dt.date.today() + dt.timedelta(days=30)).isoformat()
SEARCH = {"origin": "ARN", "destination": "LHR", "departure_date": DAY, "market": "SE"}
PERSON = {
    "title": "ms",
    "given_name": "Anna",
    "family_name": "Berg",
    "born_on": "1990-04-02",
    "gender": "f",
    "email": "anna@example.com",
    "phone_number": "+46701234567",
}
JSON_HEADERS = {"origin": ORIGIN, "x-crewgate": "1"}


@dataclass
class Console:
    st: Stack
    app: Any
    http: httpx.AsyncClient
    model: ScriptedChat
    refreshes: list[dict[str, str]]

    async def sign_in(self, user: str = "anna") -> None:
        r = await self.http.get("/login")
        assert r.status_code == 303
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["crewgate-web"]
        assert q["redirect_uri"] == [f"{ORIGIN}/callback"]
        code = f"code-{user}"
        self.st.idp.auth_codes[code] = {"sub": f"user-{user}", "scope": ALL_SCOPES, "azp": "crewgate-web"}
        r = await self.http.get("/callback", params={"code": code, "state": q["state"][0]})
        assert r.status_code == 303 and r.headers["location"] == "/"

    async def chat(self, text: str, **headers: str) -> httpx.Response:
        return await self.http.post("/api/chat", json={"message": text}, headers=headers or JSON_HEADERS)


@asynccontextmanager
async def console(steps: list[Any], **settings: Any) -> AsyncIterator[Console]:
    async with gateway_stack() as st:
        refreshes: list[dict[str, str]] = []
        inner = st.idp.handler

        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(x.split("=", 1) for x in request.content.decode().split("&") if "=" in x)
            if form.get("grant_type") == "refresh_token":
                refreshes.append(form)
                if form.get("refresh_token") == "dead":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                token = st.idp.token(sub="user-anna", scope=ALL_SCOPES, azp="crewgate-web")
                return httpx.Response(200, json={"access_token": token, "refresh_token": "r2"})
            resp = inner(request)
            if form.get("grant_type") == "authorization_code" and resp.status_code == 200:
                body = resp.json() | {"refresh_token": "r1"}
                return httpx.Response(200, json=body)
            return resp

        ws = WebSettings(
            _env_file=None,  # type: ignore[call-arg]
            public_url=ORIGIN,
            issuer=ISSUER,
            internal_issuer=ISSUER,
            gateway_mcp_url=MCP_URL,
            grafana_url="https://console.test/grafana/",
            **settings,
        )
        model = ScriptedChat(steps)
        idp_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(ws, model, idp_http=idp_http, gateway_app=st.app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            yield Console(st, app, http, model, refreshes)


def cheapest(messages: Any) -> dict[str, Any]:
    o = last_tool(messages)["offers"][0]
    return {"offer_id": o["offer_id"], "quoted_total": o["total_amount"]}


def book_args(messages: Any) -> dict[str, Any]:
    o = last_tool(messages)
    return {
        "offer_id": o["offer_id"],
        "expected_total": o["total_amount"],
        "currency": o["currency"],
        "market": "SE",
        "passengers": [PERSON],
    }


async def test_page_and_assets_carry_a_strict_policy() -> None:
    async with console([]) as c:
        for path, kind in [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")]:
            r = await c.http.get(path)
            assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
            assert "script-src 'self'" in r.headers["content-security-policy"]
        r = await c.http.get("/api/session")
        assert r.json()["signed_in"] is False and "anna" in r.json()["note"]
        assert (await c.http.get("/healthz")).json() == {"status": "ok"}


async def test_sign_in_keeps_tokens_on_the_server() -> None:
    async with console([]) as c:
        await c.sign_in()
        cookie = c.http.cookies.get("crewgate_session")
        assert cookie and "." not in cookie  # an opaque id, not a token
        s = (await c.http.get("/api/session")).json()
        assert s["signed_in"] and s["username"] == "anna" and s["turns_left"] == 20
        assert s["grafana_url"] == "https://console.test/grafana/"
        sess = c.app.state.store.get(cookie)
        assert sess.tokens.access.count(".") == 2 and sess.tokens.refresh == "r1"


async def test_bad_or_missing_sign_in_state_is_refused() -> None:
    async with console([]) as c:
        r = await c.http.get("/callback", params={"code": "x", "state": "y"})
        assert r.headers["location"] == "/?error=session_expired"
        await c.http.get("/login")
        r = await c.http.get("/callback", params={"code": "x", "state": "forged"})
        assert r.headers["location"] == "/?error=sign_in_failed"
        r = await c.http.get("/callback", params={"error": "access_denied"})
        assert r.headers["location"] == "/?error=sign_in_cancelled"
        assert (await c.http.get("/api/session")).json()["signed_in"] is False


async def test_a_token_for_another_audience_is_not_accepted() -> None:
    async with console([]) as c:
        r = await c.http.get("/login")
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        c.st.idp.auth_codes["c"] = {"sub": "user-anna", "aud": "crew-roster-api", "azp": "crewgate-web"}
        r = await c.http.get("/callback", params={"code": "c", "state": state})
        assert r.headers["location"] == "/?error=sign_in_failed"


async def test_a_turn_shows_every_step_and_the_pending_approval() -> None:
    steps = [
        call("booking_search_flights", SEARCH),
        call("booking_get_offer", cheapest),
        call("booking_book_flight", book_args),
        say("Done, your flight is booked!"),
    ]
    async with console(steps) as c:
        await c.sign_in()
        r = await c.chat("Book the cheapest ARN to LHR for Anna Berg")
        assert r.status_code == 200
        t = r.json()
        assert [s["tool"] for s in t["steps"] if s["kind"] == "tool"] == [
            "booking_search_flights",
            "booking_get_offer",
            "booking_book_flight",
        ]
        assert not t["guard"]["ok"] and t["guard"]["draft"] == "Done, your flight is booked!"
        assert t["reply"].startswith("Nothing is booked yet")
        assert len(t["approvals"]) == 1 and "/approvals/" in t["approvals"][0]["approve_url"]
        assert t["turns_left"] == 19 and t["model_calls_left_today"] == 296
        log = (await c.http.get("/api/session")).json()["log"]
        assert log[0]["reply"] == t["reply"]
        metrics = (await c.http.get("/metrics")).text
        assert 'crewgate_agent_guard_total{decision="replaced"} 1.0' in metrics
        assert 'crewgate_agent_tool_calls_total{outcome="ok",tool="booking_book_flight"} 1.0' in metrics
        assert c.st.duffel.posts("/air/orders") == []


async def test_cross_site_requests_are_refused() -> None:
    async with console([say("hi")]) as c:
        await c.sign_in()
        assert (await c.chat("hi", origin="https://evil.test", **{"x-crewgate": "1"})).status_code == 403
        assert (await c.chat("hi", origin=ORIGIN)).status_code == 403  # no custom header
        r = await c.http.post("/logout", headers={"origin": "https://evil.test"})
        assert r.status_code == 403


async def test_limits() -> None:
    async with console([say("one"), say("two")], max_turns_per_hour=1) as c:
        await c.sign_in()
        assert (await c.chat("x" * 1001)).status_code == 400
        assert (await c.chat("hello")).status_code == 200
        r = await c.chat("again")
        assert r.status_code == 429 and r.json()["error"] == "too_many_turns"
    async with console([say("one")], max_model_calls_per_day=0) as c:
        await c.sign_in()
        r = await c.chat("hello")
        assert r.status_code == 429 and r.json()["error"] == "daily_budget"


async def test_not_signed_in() -> None:
    async with console([]) as c:
        assert (await c.chat("hello")).status_code == 401
        assert (await c.http.post("/api/reset", headers=JSON_HEADERS)).status_code == 401


async def test_expired_tokens_are_refreshed_and_a_dead_refresh_signs_out() -> None:
    async with console([say("hi"), say("hi")]) as c:
        await c.sign_in()
        sess = c.app.state.store.get(c.http.cookies.get("crewgate_session"))
        sess.tokens.expires_at = 0
        assert (await c.chat("hello")).status_code == 200
        assert c.refreshes[-1]["refresh_token"] == "r1" and sess.tokens.refresh == "r2"
        sess.tokens.expires_at, sess.tokens.refresh = 0, "dead"
        assert (await c.chat("hello")).status_code == 401
        assert (await c.http.get("/api/session")).json()["signed_in"] is False


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [(LLMQuotaExceeded("daily", 1500), 503, "model_busy"), (LLMError("x"), 502, "model_error")],
)
async def test_model_failures_are_reported_plainly(error: Exception, status: int, code: str) -> None:
    def fail(_: Any) -> Completion:
        raise error

    async with console([fail]) as c:
        await c.sign_in()
        r = await c.chat("hello")
        assert r.status_code == status and r.json()["error"] == code
        if code == "model_busy":
            assert "about 25 min" in r.json()["message"]


async def test_reset_and_logout() -> None:
    async with console([say("first"), say("second")]) as c:
        await c.sign_in()
        await c.chat("hello")
        assert (await c.http.post("/api/reset", headers=JSON_HEADERS)).json() == {"ok": True}
        assert (await c.http.get("/api/session")).json()["log"] == []
        r = await c.http.post("/logout", headers=JSON_HEADERS)
        url = r.json()["logout_url"]
        assert url.startswith(f"{ISSUER}/protocol/openid-connect/logout?")
        assert "post_logout_redirect_uri=https%3A%2F%2Fconsole.test%2F" in url
        assert (await c.http.get("/api/session")).json()["signed_in"] is False


async def test_render_lists_blocked_writes_and_rewrites() -> None:
    steps = [
        call("booking_search_flights", SEARCH),
        call("booking_book_flight", {"offer_id": "off_x"}),
        say("Here."),
    ]
    async with console(steps) as c:
        await c.sign_in()
        t = (await c.chat("Show me flights from ARN to LHR")).json()
        assert {"kind": "blocked", "tool": "booking_book_flight"}.items() <= next(
            s for s in t["steps"] if s["kind"] == "blocked"
        ).items()
        assert json.dumps(t)  # everything the page gets is plain JSON


async def test_without_a_model_key_chat_is_off_but_sign_in_works() -> None:
    async with gateway_stack() as st:
        ws = WebSettings(_env_file=None, public_url=ORIGIN, issuer=ISSUER, internal_issuer=ISSUER)  # type: ignore[call-arg]
        app = create_app(ws, None, idp_http=st.idp.client(), gateway_app=st.app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            c = Console(st, app, http, ScriptedChat([]), [])
            await c.sign_in()
            assert (await http.get("/api/session")).json()["chat_enabled"] is False
            r = await c.chat("hello")
            assert r.status_code == 503 and r.json()["error"] == "model_not_configured"


async def test_an_unexpected_failure_is_a_plain_500() -> None:
    async with console([]) as c:  # the script is empty: the model raises AssertionError
        await c.sign_in()
        r = await c.chat("hello")
        assert r.status_code == 500 and r.json() == {
            "error": "internal_error",
            "message": "Something went wrong.",
        }


def test_daily_budget_and_session_expiry() -> None:
    from crewgate.web.app import DailyBudget
    from crewgate.web.sessions import SessionStore

    t = [1_800_000_000.0]
    b = DailyBudget(5, clock=lambda: t[0])
    b.spend(4)
    assert b.left == 1
    t[0] += 86_400
    assert b.left == 5  # a new UTC day
    store = SessionStore(ttl=60, clock=lambda: t[0])
    s = store.new()
    assert store.get(s.id) is s and store.get(None) is None
    t[0] += 61
    assert store.get(s.id) is None
    s2 = store.new()
    t[0] += 61
    store.new()  # sweeps the expired one
    assert len(store) == 1 and store.get(s2.id) is None
    store.drop(None)


async def test_a_proxy_rewriting_origin_does_not_break_the_page() -> None:
    """Codespaces' port forwarding rewrites Origin; the browser's Sec-Fetch-Site still tells the truth."""
    async with console([say("hi"), say("hi")]) as c:
        await c.sign_in()
        rewritten = {"origin": "http://localhost:8443", "sec-fetch-site": "same-origin", "x-crewgate": "1"}
        assert (await c.chat("hello", **rewritten)).status_code == 200
        cross = {"origin": ORIGIN, "sec-fetch-site": "cross-site", "x-crewgate": "1"}
        assert (await c.chat("hello", **cross)).status_code == 403
        assert (
            await c.chat("hello", **{"sec-fetch-site": "same-origin"})
        ).status_code == 403  # no custom header
