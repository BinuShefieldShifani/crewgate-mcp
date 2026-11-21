"""Who is calling: the verified identity the rest of the gateway works with."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from .errors import AuthError


@dataclass(frozen=True)
class Principal:
    subject: str  # stable user id (Keycloak "sub")
    client_id: str  # the app acting for the user ("azp")
    username: str | None
    scopes: frozenset[str]  # what the app was allowed to ask for
    roles: Mapping[str, frozenset[str]]  # what the user may do, per resource ("resource_access")
    expires_at: float
    token: SecretStr = field(repr=False)  # kept for OBO exchange, never logged
    auth_time: float | None = None  # when the person last actually signed in (not a token refresh)

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any], token: str) -> "Principal":
        access = claims.get("resource_access") or {}
        roles = {
            str(resource): frozenset(str(r) for r in (entry or {}).get("roles", []))
            for resource, entry in access.items()
            if isinstance(entry, dict)
        }
        return cls(
            subject=str(claims["sub"]),
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            username=claims.get("preferred_username"),
            scopes=frozenset(str(claims.get("scope", "")).split()),
            roles=roles,
            expires_at=float(claims["exp"]),
            token=SecretStr(token),
            auth_time=float(claims["auth_time"]) if claims.get("auth_time") is not None else None,
        )

    def roles_for(self, resource: str) -> frozenset[str]:
        return self.roles.get(resource, frozenset())

    def require_scopes(self, needed: Iterable[str]) -> None:
        wanted = frozenset(needed)
        if not wanted <= self.scopes:
            raise AuthError.insufficient_scope(" ".join(sorted(wanted)))
