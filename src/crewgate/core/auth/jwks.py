"""Cached JSON Web Key Set with safe refresh.

Keys are cached for `ttl_seconds`. An unknown `kid` (the IdP rotated its keys) triggers one
early refresh, but never more often than `min_refresh_interval_seconds`, so a flood of tokens
with made-up kids cannot hammer the identity provider.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .errors import AuthError

log = logging.getLogger("crewgate.auth.jwks")


@dataclass(frozen=True)
class SigningKey:
    jwk: jwt.PyJWK
    alg: str | None  # the "alg" the IdP declared for this key, if any


class JwksCache:
    def __init__(
        self,
        url: str,
        http: httpx.AsyncClient,
        ttl_seconds: float = 300.0,
        min_refresh_interval_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url, self.http, self.ttl = url, http, ttl_seconds
        self.min_interval, self.clock = min_refresh_interval_seconds, clock
        self._keys: dict[str, SigningKey] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    async def get_key(self, kid: str) -> SigningKey:
        if self._fresh() and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            if not self._fresh() or (kid not in self._keys and self._may_refresh_early()):
                await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise AuthError.invalid_token("unknown signing key")
        return key

    def _fresh(self) -> bool:
        return self._fetched_at is not None and self.clock() - self._fetched_at < self.ttl

    def _may_refresh_early(self) -> bool:
        return self._fetched_at is None or self.clock() - self._fetched_at >= self.min_interval

    async def _refresh(self) -> None:
        try:
            resp = await self.http.get(self.url)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self._keys:  # keep serving the last good keys rather than locking everyone out
                log.warning("jwks_refresh_failed_using_cached", extra={"error": type(exc).__name__})
                self._fetched_at = self.clock()
                return
            raise AuthError.unavailable("cannot load signing keys") from exc

        keys: dict[str, SigningKey] = {}
        for raw in data.get("keys", []):
            if raw.get("use", "sig") != "sig" or "kid" not in raw:
                continue  # skip encryption keys (Keycloak publishes an RSA-OAEP key too)
            try:
                keys[str(raw["kid"])] = SigningKey(jwt.PyJWK(raw), raw.get("alg"))
            except (jwt.PyJWKError, jwt.InvalidKeyError):
                log.warning("jwks_key_skipped", extra={"kid": raw.get("kid")})
        self._keys = keys
        self._fetched_at = self.clock()
        self.fetch_count += 1
