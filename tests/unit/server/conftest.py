"""A whole gateway in-process: fake IdP (real RSA keys), mock crew API in OIDC + HMAC mode.

Tests talk raw JSON-RPC over HTTP to it. No MCP SDK client in the loop, so the wire format
itself is what gets tested.
"""

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette

from crewgate.core.audit import MemorySink
from crewgate.core.auth import AuthSettings, JwksCache, TokenVerifier
from crewgate.server import Gateway, GatewaySettings, create_app
from mock_roster_api.app import create_app as create_partner
from mock_roster_api.settings import ApiSettings
from tests.helpers.idp import GATEWAY, ISSUER, JWKS_URL, PARTNER, TOKEN_URL, FakeIdp

REPO = Path(__file__).resolve().parents[3]
BASE = "http://testserver"
USERS = {
    "user-anna": {"roles": {PARTNER: ["roster-reader", "swap-requester"]}},
    "user-erik": {"roles": {PARTNER: ["roster-reader"]}},
}
_ids = itertools.count(1)


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    idp: FakeIdp
    app: Starlette
    gw: Gateway
    http: httpx.AsyncClient
    audit: MemorySink
    clock: Clock

    def agent_token(self, user: str = "anna", **kw: Any) -> str:
        """What the MCP client (the agent) holds after the planner signed in."""
        return self.idp.token(sub=f"user-{user}", **kw)

    def approval_token(self, user: str = "anna", *, signed_in_ago: float = 5, **kw: Any) -> str:
        """What the approval app gets after the person signs in again in their browser."""
        kw.setdefault("scope", "openid swap.write")
        return self.idp.token(
            sub=f"user-{user}", azp="crewgate-approvals", auth_time=int(self.clock() - signed_in_ago), **kw
        )

    async def rpc(
        self, method: str, params: dict[str, Any] | None = None, token: str | None = "default"
    ) -> httpx.Response:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": "2025-11-25",
        }
        if token == "default":
            token = self.agent_token()
        if token:
            headers["authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}}
        return await self.http.post("/mcp", json=body, headers=headers)

    async def call(self, tool: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        r = await self.rpc("tools/call", {"name": tool, "arguments": args or {}}, **kw)
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    idp = FakeIdp(users=USERS)
    clock = Clock()
    partner = create_partner(
        ApiSettings(
            auth_mode="oidc", hmac_secrets=[SecretStr("dev-only-hmac-secret")], rate_limit_per_minute=10_000
        ),
        verifier=TokenVerifier(ISSUER, PARTNER, JwksCache(JWKS_URL, idp.client())),
    )
    settings = GatewaySettings(
        public_url=BASE,
        allowed_hosts=["testserver"],
        allowed_origins=[BASE],
        entitlements_path=str(REPO / "config" / "entitlements.toml"),
        approvals_authorize_url="https://idp.test/realms/crewgate/protocol/openid-connect/auth",
    )
    auth = AuthSettings(
        issuer=ISSUER, jwks_url=JWKS_URL, token_url=TOKEN_URL, audience=GATEWAY, resource=f"{BASE}/mcp"
    )
    sink = MemorySink()
    app, gw = create_app(
        settings,
        auth,
        idp_http=idp.client(),
        partner_transport=httpx.ASGITransport(app=partner),
        audit_sink=sink,
        clock=clock,
    )
    # The MCP session manager's task group must open and close in the same task, but
    # pytest-asyncio runs fixture setup and teardown in different tasks. So the lifespan
    # runs in its own task for the whole test.
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
            yield Env(idp, app, gw, http, sink, clock)
    finally:
        stop.set()
        await task
