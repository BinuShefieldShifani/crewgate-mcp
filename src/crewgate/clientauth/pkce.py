"""MCP client sign-in: OAuth 2.1 authorization code with PKCE (RFC 7636), per the MCP spec.

The flow a client follows:
1. Call the gateway without a token, get 401 with a `resource_metadata` link.
2. Fetch the Protected Resource Metadata (RFC 9728) and pick an authorization server.
3. Discover that server's endpoints (RFC 8414, then OpenID Connect discovery).
4. Refuse to continue unless it supports S256 PKCE.
5. Send the user to the authorization endpoint with a code challenge and the `resource`
   parameter (RFC 8707), then trade the code plus the verifier for an access token.
"""

import base64
import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls, nbytes: int = 48) -> "PkcePair":
        verifier = _b64url(secrets.token_bytes(nbytes))  # 48 bytes -> 64 chars, inside 43..128
        return cls(verifier, cls.challenge_for(verifier))

    @staticmethod
    def challenge_for(verifier: str) -> str:
        if not 43 <= len(verifier) <= 128:
            raise ValueError("PKCE verifier must be 43 to 128 characters")
        return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


class DiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class AuthServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str | None
    code_challenge_methods_supported: tuple[str, ...]


def discovery_urls(issuer: str) -> list[str]:
    """Order from the MCP spec: RFC 8414 first, then the two OpenID Connect placements."""
    parts = urlsplit(issuer)
    path = parts.path.rstrip("/")

    def at(p: str) -> str:
        return urlunsplit((parts.scheme, parts.netloc, p, "", ""))

    if not path:
        return [at("/.well-known/oauth-authorization-server"), at("/.well-known/openid-configuration")]
    return [
        at(f"/.well-known/oauth-authorization-server{path}"),
        at(f"/.well-known/openid-configuration{path}"),
        at(f"{path}/.well-known/openid-configuration"),
    ]


async def discover_authorization_server(http: httpx.AsyncClient, issuer: str) -> AuthServerMetadata:
    for url in discovery_urls(issuer):
        resp = await http.get(url)
        if resp.status_code != 200:
            continue
        doc: dict[str, Any] = resp.json()
        if doc.get("issuer") != issuer:  # mix-up defence: the document must describe this issuer
            raise DiscoveryError(f"issuer mismatch at {url}")
        methods = tuple(doc.get("code_challenge_methods_supported") or ())
        if "S256" not in methods:
            raise DiscoveryError("authorization server does not support S256 PKCE")
        return AuthServerMetadata(
            issuer=issuer,
            authorization_endpoint=doc["authorization_endpoint"],
            token_endpoint=doc["token_endpoint"],
            jwks_uri=doc.get("jwks_uri"),
            code_challenge_methods_supported=methods,
        )
    raise DiscoveryError(f"no authorization server metadata for {issuer}")


async def fetch_protected_resource_metadata(http: httpx.AsyncClient, url: str) -> dict[str, Any]:
    resp = await http.get(url)
    resp.raise_for_status()
    doc: dict[str, Any] = resp.json()
    if not doc.get("authorization_servers"):
        raise DiscoveryError("protected resource metadata lists no authorization server")
    return doc


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: Iterable[str],
    state: str,
    pkce: PkcePair,
    resource: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if resource:
        params["resource"] = resource
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


class TokenRequestError(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"{status}: {error}")
        self.status, self.error = status, error


async def exchange_code(
    http: httpx.AsyncClient,
    token_endpoint: str,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    pkce: PkcePair,
    resource: str | None = None,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": pkce.verifier,
    }
    if resource:
        data["resource"] = resource
    resp = await http.post(token_endpoint, data=data)
    if resp.status_code != 200:
        try:
            error = str(resp.json().get("error", "unknown_error"))
        except ValueError:
            error = "unknown_error"
        raise TokenRequestError(resp.status_code, error)
    body: dict[str, Any] = resp.json()
    return body
