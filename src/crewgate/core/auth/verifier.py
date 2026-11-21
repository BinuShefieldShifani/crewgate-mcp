"""Access-token verification for the gateway (OAuth 2.1 resource server).

Checks, in order: token shape, algorithm allowlist (no "none", no HMAC, so a public key can't
be abused as an HMAC secret), key id, signature, issuer, audience, expiry and token type.
A token that passes becomes a Principal. Anything else becomes an AuthError with a safe message.
"""

from collections.abc import Iterable

import jwt

from .errors import AuthError
from .jwks import JwksCache
from .principal import Principal

DEFAULT_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"})
# Keycloak marks access tokens "Bearer" and ID tokens "ID". RFC 9068 tokens use "at+jwt" in the header.
_ACCESS_TOKEN_TYPES = frozenset({"Bearer", "at+jwt"})


class TokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks: JwksCache,
        algorithms: Iterable[str] = DEFAULT_ALGORITHMS,
        leeway_seconds: float = 30.0,
    ) -> None:
        self.issuer, self.audience, self.jwks = issuer, audience, jwks
        self.algorithms = frozenset(algorithms)
        if self.algorithms & {"none", "HS256", "HS384", "HS512"}:
            raise ValueError("symmetric and 'none' algorithms are not allowed for access tokens")
        self.leeway = leeway_seconds

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError.invalid_token("malformed token") from exc

        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self.algorithms:
            raise AuthError.invalid_token("unsupported signing algorithm")
        if not kid:
            raise AuthError.invalid_token("token has no key id")
        if header.get("typ") not in (None, "JWT", *_ACCESS_TOKEN_TYPES):
            raise AuthError.invalid_token("not an access token")

        signing_key = await self.jwks.get_key(str(kid))
        if signing_key.alg is not None and signing_key.alg != alg:
            raise AuthError.invalid_token("algorithm does not match signing key")

        try:
            claims = jwt.decode(
                token,
                key=signing_key.jwk.key,
                algorithms=[alg],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError.invalid_token("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError.invalid_token("token was not issued for this resource") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError.invalid_token("untrusted issuer") from exc
        except jwt.PyJWTError as exc:
            raise AuthError.invalid_token("invalid token") from exc

        if claims.get("typ") not in (None, *_ACCESS_TOKEN_TYPES):
            raise AuthError.invalid_token("not an access token")
        return Principal.from_claims(claims, token)
