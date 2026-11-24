"""The console's side of OAuth: build the sign-in redirect, swap the code, refresh, verify."""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from crewgate.clientauth.pkce import PkcePair
from crewgate.core.auth import AuthError, JwksCache, TokenVerifier

from .sessions import Tokens
from .settings import WebSettings


class SignInError(Exception):
    pass


@dataclass
class Oidc:
    settings: WebSettings
    http: httpx.AsyncClient
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        s = self.settings
        self.verifier = TokenVerifier(s.issuer, s.audience, JwksCache(s.jwks_url, self.http))

    def start(self) -> tuple[str, dict[str, str]]:
        """Where to send the browser, and what to remember until it comes back."""
        pkce = PkcePair.generate()
        login = {
            "state": secrets.token_urlsafe(24),
            "nonce": secrets.token_urlsafe(24),
            "verifier": pkce.verifier,
        }
        params = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "scope": " ".join(self.settings.scopes),
            "state": login["state"],
            "nonce": login["nonce"],
            "code_challenge": pkce.challenge,
            "code_challenge_method": pkce.method,
        }
        return f"{self.settings.authorize_url}?{urlencode(params)}", login

    async def finish(self, login: dict[str, str] | None, state: str | None, code: str | None) -> Tokens:
        if not login or not state or not code or not secrets.compare_digest(state, login["state"]):
            raise SignInError("sign-in state did not match; start again")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "code_verifier": login["verifier"],
        }
        return await self._token(data)

    async def refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh:
            raise SignInError("no refresh token; sign in again")
        return await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh})

    def needs_refresh(self, tokens: Tokens, margin: float = 30.0) -> bool:
        return tokens.expires_at - margin <= self.clock()

    async def _token(self, data: dict[str, str]) -> Tokens:
        s = self.settings
        try:
            resp = await self.http.post(
                s.token_url,
                data={**data, "client_id": s.client_id},
                auth=(s.client_id, s.client_secret.get_secret_value()),
            )
        except httpx.HTTPError as exc:
            raise SignInError("the sign-in server is unreachable") from exc
        if resp.status_code != 200:
            raise SignInError(f"the sign-in server refused ({resp.status_code})")
        body = resp.json()
        access = str(body.get("access_token") or "")
        try:
            principal = await self.verifier.verify(access)  # same checks the gateway makes
        except AuthError as exc:
            raise SignInError("the sign-in server returned a token this console won't use") from exc
        return Tokens(
            access=access,
            refresh=body.get("refresh_token"),
            expires_at=principal.expires_at,
            username=principal.username or principal.subject,
            subject=principal.subject,
        )
