"""Thin client for the Duffel Flights API (test mode), on crewgate's shared PartnerClient.

What it adds on top of PartnerClient:
- the headers Duffel requires (Duffel-Version, Accept, gzip) and the {"data": ...} envelope
- supplier_timeout, so one slow airline can't stall a tool call
- Duffel error codes turned into crewgate categories (price_changed is a conflict, not bad input)
- searches marked safe to retry; creating an order is never retried after it may have been sent,
  because Duffel documents no idempotency key for orders
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import httpx

from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.metrics import GatewayMetrics
from crewgate.core.partner_http import PartnerClient, RetryPolicy

from .config import DuffelSettings

# Duffel codes that mean "the world changed", not "your input was wrong".
_CODE_CATEGORY = {
    "price_changed": ErrorCategory.CONFLICT,
    "offer_no_longer_available": ErrorCategory.CONFLICT,
    "offer_expired": ErrorCategory.CONFLICT,
    "offer_request_already_booked": ErrorCategory.CONFLICT,
    "order_creation_already_attempted": ErrorCategory.CONFLICT,
    "duplicate_booking": ErrorCategory.CONFLICT,
    "insufficient_balance": ErrorCategory.CONFLICT,
    "airline_internal": ErrorCategory.UPSTREAM_UNAVAILABLE,
    "airline_unknown": ErrorCategory.UPSTREAM_UNAVAILABLE,
    "invalid_order": ErrorCategory.UPSTREAM_UNAVAILABLE,
    "rate_limit_exceeded": ErrorCategory.RATE_LIMITED,
    "not_found": ErrorCategory.NOT_FOUND,
    "access_token_not_found": ErrorCategory.AUTH,
    "expired_access_token": ErrorCategory.AUTH,
    "insufficient_permissions": ErrorCategory.AUTH,
}


class NotConfigured(PartnerError):
    def __init__(self) -> None:
        super().__init__(ErrorCategory.AUTH, "booking_not_configured", code="booking_not_configured")


class _StaticToken:
    refreshable = False

    def __init__(self, settings: DuffelSettings) -> None:
        self._token = settings.access_token

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        if self._token is None:
            raise NotConfigured()
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}


def _remap(exc: PartnerError) -> PartnerError:
    category = _CODE_CATEGORY.get(exc.code or "")
    if category is None or category is exc.category:
        return exc
    return PartnerError(category, exc.message, exc.status, exc.code)


class DuffelClient:
    def __init__(
        self,
        settings: DuffelSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        metrics: GatewayMetrics | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.s = settings or DuffelSettings()
        self.http = httpx.AsyncClient(
            base_url=self.s.base_url,
            transport=transport,
            timeout=self.s.timeout_seconds,
            headers={
                "Duffel-Version": self.s.api_version,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        self.partner = PartnerClient(
            self.http,
            _StaticToken(self.s),
            policy=RetryPolicy(max_retries=2, max_retry_after_seconds=self.s.max_retry_after_seconds),
            sleep=sleep,
            name="duffel",
            metrics=metrics,
        )

    @property
    def configured(self) -> bool:
        return self.s.access_token is not None

    async def close(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "DuffelClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await self.partner.request(method, path, **kwargs)
        except PartnerError as exc:
            raise _remap(exc) from exc

    async def search(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: date,
        adults: int,
        cabin_class: str,
        max_connections: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        body = {
            "data": {
                "slices": [
                    {
                        "origin": origin,
                        "destination": destination,
                        "departure_date": departure_date.isoformat(),
                    }
                ],
                "passengers": [{"type": "adult"} for _ in range(adults)],
                "cabin_class": cabin_class,
                "max_connections": max_connections,
            }
        }
        params = {"return_offers": "false", "supplier_timeout": str(self.s.supplier_timeout_ms)}
        # A search has no side effects, so it is safe to retry even though it is a POST.
        request = await self._call("POST", "/air/offer_requests", params=params, json=body, idempotent=True)
        offer_request = request["data"]
        page = await self._call(
            "GET",
            "/air/offers",
            params={"offer_request_id": offer_request["id"], "limit": str(self.s.max_offers)},
        )
        return offer_request, list(page.get("data") or [])

    async def get_offer(self, offer_id: str) -> dict[str, Any]:
        body = await self._call(
            "GET", f"/air/offers/{offer_id}", params={"return_available_services": "true"}
        )
        offer: dict[str, Any] = body["data"]
        return offer

    async def seat_maps(self, offer_id: str) -> list[dict[str, Any]]:
        body = await self._call("GET", "/air/seat_maps", params={"offer_id": offer_id})
        return list((body or {}).get("data") or [])

    async def create_order(
        self,
        *,
        offer_id: str,
        passengers: list[dict[str, Any]],
        amount: str,
        currency: str,
        metadata: dict[str, str],
    ) -> dict[str, Any] | None:
        """Instant order paid from the test balance. None means accepted but not yet created (202)."""
        body = {
            "data": {
                "type": "instant",
                "selected_offers": [offer_id],
                "passengers": passengers,
                "payments": [{"type": "balance", "currency": currency, "amount": amount}],
                "metadata": metadata,
            }
        }
        result = await self._call("POST", "/air/orders", json=body)  # never retried after sending
        data = (result or {}).get("data") if isinstance(result, dict) else None
        return data if isinstance(data, dict) and data.get("id") else None

    async def get_order(self, order_id: str) -> dict[str, Any]:
        body = await self._call("GET", f"/air/orders/{order_id}")
        order: dict[str, Any] = body["data"]
        return order


def today() -> date:
    return date.fromtimestamp(time.time())
