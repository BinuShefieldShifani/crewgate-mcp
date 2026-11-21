"""A tiny fake identity provider for unit tests: real RSA/EC keys, real signatures, in-memory JWKS.

It lets the verifier, JWKS cache and OBO exchanger be tested without running Keycloak.
"""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = "https://idp.test/realms/crewgate"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
GATEWAY = "crewgate-gateway"
PARTNER = "crew-roster-api"


@dataclass
class SigningKeyPair:
    kid: str
    alg: str
    private: Any
    jwk: dict[str, Any]


def rsa_key(kid: str = "rsa-1", alg: str | None = "RS256") -> SigningKeyPair:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig"})
    if alg:
        jwk["alg"] = alg
    return SigningKeyPair(kid, alg or "RS256", private, jwk)


def ec_key(kid: str = "ec-1") -> SigningKeyPair:
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return SigningKeyPair(kid, "ES256", private, jwk)


@dataclass
class FakeIdp:
    keys: list[SigningKeyPair] = field(default_factory=lambda: [rsa_key()])
    jwks_calls: int = 0
    jwks_status: int = 200
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    exchange_calls: list[dict[str, str]] = field(default_factory=list)
    exchange_response: Callable[[dict[str, str]], httpx.Response] | None = None
    partner_ttl: int = 300

    # ---- tokens -------------------------------------------------------------------------
    def token(
        self,
        *,
        sub: str = "user-anna",
        aud: str | list[str] = GATEWAY,
        scope: str = "openid roster.read swap.write",
        roles: dict[str, list[str]] | None = None,
        exp_in: int = 300,
        key: SigningKeyPair | None = None,
        alg: str | None = None,
        headers: dict[str, Any] | None = None,
        drop: tuple[str, ...] = (),
        **extra: Any,
    ) -> str:
        k = key or self.keys[0]
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": sub,
            "aud": aud,
            "azp": "crewgate-mcp-client",
            "iat": now,
            "exp": now + exp_in,
            "scope": scope,
            "preferred_username": sub.removeprefix("user-"),
            "typ": "Bearer",
            "jti": uuid.uuid4().hex,
            **extra,
        }
        if roles:
            claims["resource_access"] = {r: {"roles": v} for r, v in roles.items()}
        for name in drop:
            claims.pop(name, None)
        hdr = {"kid": k.kid, **(headers or {})}
        return jwt.encode(claims, k.private, algorithm=alg or k.alg, headers=hdr)

    # ---- HTTP endpoints ------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            self.jwks_calls += 1
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status)
            return httpx.Response(200, json={"keys": [k.jwk for k in self.keys]})
        if str(request.url) == TOKEN_URL:
            form = dict(parse_qsl(request.content.decode()))
            self.exchange_calls.append(form)
            if self.exchange_response:
                return self.exchange_response(form)
            return self._exchange(form)
        return httpx.Response(404)

    def _exchange(self, form: dict[str, str]) -> httpx.Response:
        subject = jwt.decode(form["subject_token"], options={"verify_signature": False})
        user_roles = self.users.get(subject["sub"], {}).get("roles", {})
        token = self.token(
            sub=subject["sub"],
            aud=form["audience"],
            scope=form.get("scope", ""),
            roles=user_roles,
            exp_in=self.partner_ttl,
            azp=GATEWAY,
        )
        return httpx.Response(
            200, json={"access_token": token, "expires_in": self.partner_ttl, "token_type": "Bearer"}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())
