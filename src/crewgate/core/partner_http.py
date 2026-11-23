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
from email.utils import parsedate_to_datetime
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
    # Longest Retry-After we will sit through. A partner asking for more gets an immediate
    # rate_limited error instead: an agent's tool call can't block for half a minute.
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

    async def request(self, method: str, path: str, *, idempotent: bool | None = None, **kwargs: Any) -> Any:
        """Send with retries that can't cause duplicates.

        `idempotent` defaults to the HTTP method's meaning (GET, PUT, DELETE...: yes; POST: no).
        Pass idempotent=True for a POST that is safe to repeat, such as a search.
        Non-idempotent calls are retried only when the request provably never reached the partner
        (connection failed), after a 429, or after a 401 refresh. A timeout after sending becomes
        OUTCOME_UNKNOWN, and a 5xx is not retried, because the partner may have acted on it.
        """
        safe = idempotent if idempotent is not None else method.upper() in _IDEMPOTENT_METHODS
        extra_headers = kwargs.pop("headers", {})
        refreshed = False
        attempt = 0
        while True:
            headers = {**extra_headers, **await self.auth.headers()}
            try:
                resp = await self.http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                self._count(None)
                never_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout)
                if not safe and not never_sent:
                    self._count_retry("not_retried_unsafe")
                    raise PartnerError(ErrorCategory.OUTCOME_UNKNOWN, type(exc).__name__) from exc
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
                wait = _retry_after(resp)
                if wait > self.policy.max_retry_after_seconds:
                    self._count_retry("429_over_budget")
                    raise PartnerError(ErrorCategory.RATE_LIMITED, "rate_limited", 429)
                self.retry_log.append(RetryEvent(429, wait))
                self._count_retry("429")
                log.warning("partner_rate_limited", extra={"path": path, "wait": wait})
                await self.sleep(wait)
                attempt += 1
                continue
            if resp.status_code >= 500 and safe and attempt < self.policy.max_retries:
                self._count_retry("5xx")
                await self._backoff(attempt, resp.status_code)
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise PartnerError.from_status(resp.status_code, _detail(resp), _code(resp))
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


_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def _seconds_until(value: str, now: float) -> float | None:
    """Accept delta-seconds, a unix timestamp or an HTTP date. None if unreadable."""
    value = value.strip()
    try:
        n = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, when.timestamp() - now)
    return max(0.0, n - now) if n > 1e9 else max(0.0, n)


def _retry_after(resp: httpx.Response, now: float | None = None) -> float:
    """Retry-After first; Duffel sends ratelimit-reset instead. Unreadable means 1 second."""
    now = time.time() if now is None else now
    for header in ("Retry-After", "ratelimit-reset"):
        if header in resp.headers:
            seconds = _seconds_until(resp.headers[header], now)
            if seconds is not None:
                return seconds
    return 1.0


def _code(resp: httpx.Response) -> str | None:
    """The partner's machine-readable error code, if it sends one (Duffel: errors[0].code)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict) and errors[0].get("code"):
            return str(errors[0]["code"])
        if isinstance(body.get("detail"), str):
            return str(body["detail"])
    return None


def _detail(resp: httpx.Response) -> str:
    code = _code(resp)
    if code:
        return code[:200]
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
