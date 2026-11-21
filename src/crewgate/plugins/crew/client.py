"""Typed client for the crew-roster partner API, built on the core PartnerClient."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx

from crewgate.core.metrics import GatewayMetrics
from crewgate.core.partner_http import AuthStrategy, ClientCredentials, PartnerClient, RetryPolicy
from crewgate.core.signing import RequestSigner, SigningAuth

from .config import CrewSettings


class RosterClient:
    def __init__(
        self,
        settings: CrewSettings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        auth: AuthStrategy | None = None,
        metrics: GatewayMetrics | None = None,
    ) -> None:
        """`auth` defaults to client credentials (M2M). Pass OnBehalfOf to act for a signed-in user."""
        self.settings = s = settings or CrewSettings()
        signing = SigningAuth(RequestSigner(s.hmac_secret, clock=clock)) if s.hmac_secret else None
        self.http = httpx.AsyncClient(
            base_url=s.api_base_url, transport=transport, timeout=s.timeout_seconds, auth=signing
        )
        self.auth: AuthStrategy = auth or ClientCredentials(
            self.http, s.token_url, s.client_id, s.client_secret, s.scopes, clock=clock
        )
        policy = RetryPolicy(max_retry_after_seconds=s.max_retry_after_seconds)
        self.partner = PartnerClient(
            self.http, self.auth, policy=policy, sleep=sleep, name="crew-roster", metrics=metrics
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "RosterClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def iter_crew(self, page_size: int = 25) -> AsyncIterator[dict[str, Any]]:
        return self.partner.paginate("/crew", params={"limit": page_size})

    async def get_roster(self, crew_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.partner.request("GET", f"/crew/{crew_id}/roster")
        return result

    async def request_swap(self, requester_id: str, partner_id: str, day: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.partner.request(
            "POST", "/swaps", json={"requester_id": requester_id, "partner_id": partner_id, "date": day}
        )
        return result
