"""On-behalf-of (OBO) token exchange, RFC 8693.

The gateway swaps the user's token (audience: crewgate-gateway) for a partner token
(audience: the partner API) that still carries the user's identity and roles. The partner then
decides what this user may do, so the gateway can never act with more power than the user.

Two rules the identity provider does not enforce for us:
- Never up-scope. Keycloak grants any optional scope of the gateway client, even one the user
  never consented to (see tests/integration/test_obo.py). We only request scopes the user's
  own token already has.
- Never pass the user's token through to a partner (MCP spec: no token passthrough).
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

import httpx
from pydantic import SecretStr

from ..errors import ErrorCategory, PartnerError
from .errors import AuthError
from .principal import Principal

log = logging.getLogger("crewgate.auth.obo")

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # nosec B105 a URN, not a secret

_CacheKey = tuple[str, str, str]


class TokenExchanger:
    def __init__(
        self,
        http: httpx.AsyncClient,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        refresh_margin_seconds: float = 30.0,
        max_cache_entries: int = 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.http, self.token_url = http, token_url
        self.client_id, self.client_secret = client_id, client_secret
        self.margin, self.max_entries, self.clock = refresh_margin_seconds, max_cache_entries, clock
        self._cache: OrderedDict[_CacheKey, tuple[str, float]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.exchange_count = 0

    async def exchange(
        self, principal: Principal, audience: str, scopes: Iterable[str], *, force: bool = False
    ) -> str:
        wanted = frozenset(scopes)
        missing = wanted - principal.scopes
        if missing:
            raise AuthError.insufficient_scope(
                " ".join(sorted(wanted)), f"user token lacks scope: {' '.join(sorted(missing))}"
            )

        subject = principal.token.get_secret_value()
        key = (hashlib.sha256(subject.encode()).hexdigest(), audience, " ".join(sorted(wanted)))
        async with self._lock:
            hit = self._cache.get(key)
            if hit and not force and self.clock() < hit[1] - self.margin:
                self._cache.move_to_end(key)
                return hit[0]
            token, expires_at = await self._request(subject, audience, wanted)
            # A partner token must never outlive the user's own session.
            self._cache[key] = (token, min(expires_at, principal.expires_at))
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return token

    async def _request(self, subject: str, audience: str, scopes: frozenset[str]) -> tuple[str, float]:
        data = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": audience,
        }
        if scopes:
            data["scope"] = " ".join(sorted(scopes))
        try:
            resp = await self.http.post(
                self.token_url, data=data, auth=(self.client_id, self.client_secret.get_secret_value())
            )
        except httpx.TransportError as exc:
            raise PartnerError(ErrorCategory.UPSTREAM_UNAVAILABLE, "identity provider unreachable") from exc

        if resp.status_code == 200:
            body = resp.json()
            self.exchange_count += 1
            log.info("obo_exchanged", extra={"audience": audience, "expires_in": body.get("expires_in")})
            return str(body["access_token"]), self.clock() + float(body.get("expires_in", 60))

        error = _oauth_error(resp)
        if resp.status_code >= 500:
            raise PartnerError(
                ErrorCategory.UPSTREAM_UNAVAILABLE, "identity provider error", resp.status_code
            )
        if error in {"invalid_grant", "invalid_token"}:
            raise AuthError.invalid_token("user session is no longer valid")
        if error == "invalid_scope":
            raise AuthError.insufficient_scope(
                " ".join(sorted(scopes)), "identity provider refused the scope"
            )
        # invalid_client, unauthorized_client, access_denied: the gateway itself is misconfigured.
        raise PartnerError(ErrorCategory.AUTH, f"token exchange rejected: {error}", resp.status_code)


def _oauth_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", "unknown_error"))
    except ValueError:
        return "unknown_error"


class OnBehalfOf:
    """AuthStrategy for PartnerClient: every partner call carries an OBO token for this user."""

    refreshable = True

    def __init__(
        self, exchanger: TokenExchanger, principal: Principal, audience: str, scopes: Iterable[str]
    ) -> None:
        self.exchanger, self.principal, self.audience = exchanger, principal, audience
        self.scopes = frozenset(scopes)

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        token = await self.exchanger.exchange(self.principal, self.audience, self.scopes, force=force_refresh)
        return {"Authorization": f"Bearer {token}"}
