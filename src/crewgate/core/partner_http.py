"""Resilient HTTP client for any partner API a plugin talks to.

It handles the parts that break agent integrations in production:
- pluggable auth (OAuth2 client credentials, static bearer token, none)
- one forced token refresh on 401, never a loop
- 429 handling that honours Retry-After, capped, with a retry budget
- exponential backoff with jitter on 5xx and network errors
- cursor pagination as an async iterator
- every failure mapped to a PartnerError category

Clock and sleep are injectable so tests can move time without waiting.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from .errors import ErrorCategory, PartnerError
from .metrics import GatewayMetrics

log = logging.getLogger("crewgate.partner_http")

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class AuthStrategy(Protocol):
    async def headers(self, force_refresh: bool = False) -> dict[str, str]: ...

    @property
    def refreshable(self) -> bool: ...


class NoAuth:
    refreshable = False

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        return {}


class StaticBearer:
    """For partners that issue long-lived API tokens (Duffel test tokens, for example)."""

    refreshable = False

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}


class ClientCredentials:
    """OAuth2 client-credentials (M2M). Caches the token and refreshes it before expiry."""

    refreshable = True

    def __init__(
        self,
        http: httpx.AsyncClient,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scopes: str = "",
        refresh_margin_seconds: float = 30.0,
        clock: Clock = time.time,
    ) -> None:
        self.http, self.token_url = http, token_url
        self.client_id, self.client_secret, self.scopes = client_id, client_secret, scopes
        self.margin, self.clock = refresh_margin_seconds, clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    def _fresh(self) -> bool:
        return self._token is not None and self.clock() < self._expires_at - self.margin

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        async with self._lock:
            if force_refresh or not self._fresh():
                await self._fetch()
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch(self) -> None:
        resp = await self.http.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret.get_secret_value(),
                "scope": self.scopes,
            },
        )
        if resp.status_code != 200:
            raise PartnerError(ErrorCategory.AUTH, "token request rejected", resp.status_code)
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self.clock() + float(body["expires_in"])
        self.fetch_count += 1
        log.info("token_fetched", extra={"client_id": self.client_id, "expires_in": body["expires_in"]})


@dataclass
class RetryPolicy:
    max_retries: int = 4
    max_retry_after_seconds: float = 30.0
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 16.0


@dataclass
class RetryEvent:
    status: int
    waited: float


@dataclass
class PartnerClient:
    """Thin, typed wrapper around httpx with auth, retries and error mapping."""

    http: httpx.AsyncClient
    auth: AuthStrategy = field(default_factory=NoAuth)
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Sleep = asyncio.sleep
    retry_log: list[RetryEvent] = field(default_factory=list)
    name: str = "partner"  # metrics label, e.g. "crew-roster" or "duffel"
    metrics: GatewayMetrics | None = None

    def _count(self, status: int | None) -> None:
        if self.metrics is not None:
            cls = f"{status // 100}xx" if status else "network_error"
            self.metrics.partner_requests.labels(self.name, cls).inc()

    def _count_retry(self, cause: str) -> None:
        if self.metrics is not None:
            self.metrics.partner_retries.labels(self.name, cause).inc()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        refreshed = False
        attempt = 0
        while True:
            headers = {**kwargs.pop("headers", {}), **await self.auth.headers()}
            try:
                resp = await self.http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                self._count(None)
                if attempt >= self.policy.max_retries:
                    raise PartnerError(ErrorCategory.UPSTREAM_UNAVAILABLE, type(exc).__name__) from exc
                self._count_retry("network")
                await self._backoff(attempt, 0)
                attempt += 1
                continue

            self._count(resp.status_code)
            if resp.status_code == 401 and self.auth.refreshable and not refreshed:
                refreshed = True
                self._count_retry("401_refresh")
                await self.auth.headers(force_refresh=True)
                continue
            if resp.status_code == 429 and attempt < self.policy.max_retries:
                wait = min(_retry_after(resp), self.policy.max_retry_after_seconds)
                self.retry_log.append(RetryEvent(429, wait))
                self._count_retry("429")
                log.warning("partner_rate_limited", extra={"path": path, "wait": wait})
                await self.sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 500 and attempt < self.policy.max_retries:
                self._count_retry("5xx")
                await self._backoff(attempt, resp.status_code)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise PartnerError.from_status(resp.status_code, _detail(resp))
            return resp.json() if resp.content else None

    async def paginate(
        self,
        path: str,
        *,
        items_key: str = "items",
        cursor_param: str = "cursor",
        next_cursor_key: str = "next_cursor",
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        base = dict(params or {})
        while True:
            query = {**base, **({cursor_param: cursor} if cursor else {})}
            page = await self.request("GET", path, params=query)
            for item in page[items_key]:
                yield item
            cursor = _dig(page, next_cursor_key)
            if not cursor:
                return

    async def _backoff(self, attempt: int, status: int) -> None:
        cap = min(self.policy.backoff_base_seconds * 2**attempt, self.policy.backoff_cap_seconds)
        wait = cap * (0.5 + random.random() / 2)  # noqa: S311  # nosec B311 jitter, not crypto
        self.retry_log.append(RetryEvent(status, wait))
        await self.sleep(wait)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("errors") or body)[:200]
    return str(body)[:200]


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    """Read 'meta.after' style keys so Duffel-style pagination works too."""
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur
