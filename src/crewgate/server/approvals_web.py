"""Approval pages and API. The only way a write tool ever runs.

Browser flow (what a planner does):
  GET  /approvals/{id}                 page: what will happen, Approve / Reject links
  GET  /approvals/{id}/start?decision= fresh sign-in at Keycloak with the approval app
                                       (PKCE, prompt=login, max_age=0)
  GET  /approvals/callback             code -> token -> checks -> run or drop the write

API (same checks, for scripted tests and tooling a person drives):
  POST /approvals/{id}/decision        Bearer token from the approval app, {"decision": "approve"}

The agent's own token (client crewgate-mcp-client) is refused here: approval needs the person
to sign in again, somewhere the agent has no credentials.
"""

import html
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from crewgate.clientauth import PkcePair, TokenRequestError, exchange_code
from crewgate.core.approvals import ApprovalError, PendingAction
from crewgate.core.auth import AuthError, AuthSettings, TokenVerifier
from crewgate.core.errors import PartnerError
from crewgate.core.guard import ToolGuard
from crewgate.core.plugin import ToolSpec

from .auth import principal_of
from .settings import GatewaySettings

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


@dataclass
class _Login:
    approval_id: str
    approve: bool
    pkce: PkcePair
    started: float


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    style = (
        "body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;"
        "padding:0 1rem;line-height:1.5}"
        ".b{display:inline-block;padding:.6rem 1.2rem;margin-right:.6rem;border-radius:.4rem;"
        "text-decoration:none;color:#fff}.ok{background:#1f7a3a}.no{background:#a32020}"
        "code{background:#eee;padding:.1rem .3rem}"
    )
    doc = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{style}</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_SECURITY_HEADERS)


def _gone(exc: ApprovalError) -> HTMLResponse:
    return _page("Nothing to approve", f"<p>This request is {html.escape(exc.reason.value)}.</p>", exc.status)


class ApprovalsWeb:
    def __init__(
        self,
        guard: ToolGuard,
        specs: dict[str, ToolSpec],
        verifier: TokenVerifier,
        http: httpx.AsyncClient,
        settings: GatewaySettings,
        auth: AuthSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.guard, self.specs, self.verifier, self.http = guard, specs, verifier, http
        self.s, self.auth, self.clock = settings, auth, clock
        self.callback = f"{settings.public_url.rstrip('/')}/approvals/callback"
        self._logins: dict[str, _Login] = {}

    def routes(self) -> list[Route]:
        return [
            Route("/approvals/callback", self.finish, methods=["GET"]),
            Route("/approvals/{approval_id}", self.show, methods=["GET"]),
            Route("/approvals/{approval_id}/start", self.start, methods=["GET"]),
            Route("/approvals/{approval_id}/decision", self.api_decide, methods=["POST"]),
        ]

    # ---- browser --------------------------------------------------------------------------

    async def show(self, request: Request) -> Response:
        try:
            action = self.guard.approvals.open(request.path_params["approval_id"])
        except ApprovalError as exc:
            return _page(
                "Nothing to approve", f"<p>This request is {html.escape(exc.reason.value)}.</p>", exc.status
            )
        base = f"/approvals/{html.escape(action.id)}/start?decision="
        minutes = max(1, int((action.expires_at - self.clock()) // 60))
        body = (
            "<p>An assistant asked to do this on your behalf:</p>"
            f"<p><strong>{html.escape(action.summary)}</strong></p>"
            f"<p>Tool <code>{html.escape(action.tool)}</code>. Expires in about {minutes} min. "
            "You will be asked to sign in again before anything happens.</p>"
            f'<a class="b ok" href="{base}approve">Approve</a><a class="b no" href="{base}reject">Reject</a>'
        )
        return _page("Approve this change?", body)

    async def start(self, request: Request) -> Response:
        approval_id = request.path_params["approval_id"]
        decision = request.query_params.get("decision")
        if decision not in ("approve", "reject"):
            return _page("Bad request", "<p>Unknown decision.</p>", 400)
        try:
            action = self.guard.approvals.open(approval_id)
        except ApprovalError as exc:
            return _page(
                "Nothing to approve", f"<p>This request is {html.escape(exc.reason.value)}.</p>", exc.status
            )
        self._purge()
        state = secrets.token_urlsafe(24)
        pkce = PkcePair.generate()
        self._logins[state] = _Login(approval_id, decision == "approve", pkce, self.clock())
        params = {
            "response_type": "code",
            "client_id": self.s.approvals_client_id,
            "redirect_uri": self.callback,
            "scope": f"openid {self.specs[action.tool].required_scope}",
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
            "prompt": "login",  # always ask for the password again
            "max_age": "0",
        }
        return RedirectResponse(
            f"{self.s.approvals_authorize_url}?{urlencode(params)}", 303, headers=_SECURITY_HEADERS
        )

    async def finish(self, request: Request) -> Response:
        login = self._logins.pop(request.query_params.get("state", ""), None)
        if login is None:
            return _page("Sign-in not recognised", "<p>Start again from the approval link.</p>", 400)
        if "error" in request.query_params or "code" not in request.query_params:
            return _page("Sign-in cancelled", "<p>Nothing was changed.</p>", 400)
        try:
            tokens = await exchange_code(
                self.http,
                self.auth.token_url,
                client_id=self.s.approvals_client_id,
                code=request.query_params["code"],
                redirect_uri=self.callback,
                pkce=login.pkce,
                client_secret=self.s.approvals_client_secret.get_secret_value(),
            )
            approver = await self.verifier.verify(str(tokens["access_token"]))
        except (TokenRequestError, AuthError):
            return _page("Sign-in failed", "<p>Nothing was changed.</p>", 400)
        status, outcome = await self._decide(login.approval_id, approver, login.approve)
        title = {"executed": "Done", "rejected": "Rejected"}.get(str(outcome.get("status")), "Not done")
        text = html.escape(str(outcome.get("summary") or outcome.get("error", "")))
        state = html.escape(str(outcome.get("status", outcome.get("error"))))
        return _page(
            title,
            f"<p>{text}</p><p>Status: <code>{state}</code></p>",
            status,
        )

    # ---- API ------------------------------------------------------------------------------

    async def api_decide(self, request: Request) -> Response:
        approver = principal_of(request.scope)
        if approver is None:
            return JSONResponse({"error": "invalid_token"}, 401, headers={"WWW-Authenticate": "Bearer"})
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("decision") not in ("approve", "reject"):
            return JSONResponse(
                {"error": "invalid_request", "message": "decision must be approve or reject"}, 400
            )
        status, outcome = await self._decide(
            request.path_params["approval_id"], approver, body["decision"] == "approve"
        )
        return JSONResponse(outcome, status)

    # ---- shared ---------------------------------------------------------------------------

    async def _decide(self, approval_id: str, approver: Any, approve: bool) -> tuple[int, dict[str, Any]]:
        try:
            action = self.guard.approvals.get(approval_id)
            done: PendingAction = await self.guard.decide(
                approval_id, approver, self.specs[action.tool], approve=approve
            )
        except ApprovalError as exc:
            return exc.status, {"error": exc.reason.value}
        except PartnerError as exc:
            return 502, {"error": f"partner_{exc.category.value}", "status": "failed"}
        except AuthError as exc:
            return exc.status, {"error": exc.code.value, "status": "failed"}
        out = done.public()
        if isinstance(done.result, BaseModel):
            out["result"] = done.result.model_dump(mode="json")
        return 200, out

    def _purge(self) -> None:
        cutoff = self.clock() - 600
        for state in [k for k, v in self._logins.items() if v.started < cutoff]:
            del self._logins[state]
