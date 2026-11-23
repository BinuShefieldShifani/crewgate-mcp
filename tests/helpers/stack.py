"""A whole crewgate gateway in one process, for agent tests and the eval harness.

Same fakes as the server tests: a fake IdP with real RSA keys, the mock crew API in OIDC + HMAC
mode, and FakeDuffel. Nothing leaves the process, so runs are fast and repeatable.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from starlette.applications import Starlette

from crewgate.core.audit import MemorySink
from crewgate.core.auth import AuthSettings, JwksCache, TokenVerifier
from crewgate.server import Gateway, GatewaySettings, create_app
from mock_roster_api.app import create_app as create_partner
from mock_roster_api.settings import ApiSettings

from .fake_duffel import FakeDuffel, RoutingTransport
from .idp import GATEWAY, ISSUER, JWKS_URL, PARTNER, TOKEN_URL, FakeIdp

REPO = Path(__file__).resolve().parents[2]
BASE = "http://testserver"
MCP_URL = f"{BASE}/mcp"
USERS = {
    "user-anna": {"roles": {PARTNER: ["roster-reader", "swap-requester"]}},
    "user-erik": {"roles": {PARTNER: ["roster-reader"]}},
}
ALL_SCOPES = "openid roster.read swap.write booking.read booking.write"
WEBHOOK_SECRET = "dev-only-duffel-webhook-secret"


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


@dataclass
class Stack:
    app: Starlette
    gw: Gateway
    idp: FakeIdp
    duffel: FakeDuffel
    clock: Clock
    audit: MemorySink
    http: httpx.AsyncClient
    variant: str = "guided"
    extra: dict[str, Any] = field(default_factory=dict)

    def agent_token(self, user: str = "anna", scope: str = ALL_SCOPES, **kw: Any) -> str:
        return self.idp.token(sub=f"user-{user}", scope=scope, **kw)

    def approval_token(self, user: str = "anna", scope: str = "openid booking.write", **kw: Any) -> str:
        """What the approval page gets after the person signs in again in their browser."""
        kw.setdefault("auth_time", int(self.clock() - 5))
        return self.idp.token(sub=f"user-{user}", azp="crewgate-approvals", scope=scope, **kw)

    async def decide(self, approval_id: str, decision: str = "approve", **kw: Any) -> httpx.Response:
        """The person approves (or rejects) out of band, as on the approval page."""
        token = kw.pop("token", None) or self.approval_token(**kw)
        return await self.http.post(
            f"/approvals/{approval_id}/decision",
            json={"decision": decision},
            headers={"authorization": f"Bearer {token}"},
        )


@contextlib.asynccontextmanager
async def gateway_stack(variant: str = "guided", duffel: FakeDuffel | None = None) -> AsyncIterator[Stack]:
    """Start the gateway with every plugin. The env vars only hold dev-only placeholders."""
    with _env(
        DUFFEL_ACCESS_TOKEN="duffel_test_" + "x" * 20,  # placeholder, never a real token
        DUFFEL_WEBHOOK_SECRETS=f'["{WEBHOOK_SECRET}"]',
    ):
        duffel = duffel or FakeDuffel()
        idp, clock, sink = FakeIdp(users=USERS), Clock(), MemorySink()
        partner = create_partner(
            ApiSettings(
                auth_mode="oidc",
                hmac_secrets=[SecretStr("dev-only-hmac-secret")],
                rate_limit_per_minute=10_000,
            ),
            verifier=TokenVerifier(ISSUER, PARTNER, JwksCache(JWKS_URL, idp.client())),
        )
        settings = GatewaySettings(
            public_url=BASE,
            allowed_hosts=["testserver"],
            allowed_origins=[BASE],
            entitlements_path=str(REPO / "config" / "entitlements.toml"),
            approvals_authorize_url="https://idp.test/realms/crewgate/protocol/openid-connect/auth",
            tool_descriptions=variant,
        )
        auth = AuthSettings(
            issuer=ISSUER, jwks_url=JWKS_URL, token_url=TOKEN_URL, audience=GATEWAY, resource=MCP_URL
        )
        app, gw = create_app(
            settings,
            auth,
            idp_http=idp.client(),
            partner_transport=RoutingTransport(
                {"localhost": httpx.ASGITransport(app=partner), "api.duffel.com": duffel.transport()}
            ),
            audit_sink=sink,
            clock=clock,
        )
        # The MCP session manager's task group must open and close in one task; pytest-asyncio
        # runs fixture setup and teardown in different tasks, so the lifespan gets its own.
        started, stop = asyncio.Event(), asyncio.Event()

        async def serve() -> None:
            async with app.router.lifespan_context(app):
                started.set()
                await stop.wait()

        task = asyncio.create_task(serve())
        await started.wait()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
                yield Stack(app, gw, idp, duffel, clock, sink, http, variant)
        finally:
            stop.set()
            await task


@contextlib.contextmanager
def _env(**values: str) -> Any:
    import os

    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
