"""Fixtures for tests that run against the real compose stack (Keycloak + mock crew API).

Start the stack first:  make up      Then run:  make integration
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from crewgate.clientauth import AuthServerMetadata, discover_authorization_server
from crewgate.clientauth.devlogin import headless_login
from crewgate.core.auth import JwksCache, Principal, TokenExchanger, TokenVerifier

KEYCLOAK = os.getenv("KEYCLOAK_URL", "http://localhost:8080")
ISSUER = f"{KEYCLOAK}/realms/crewgate"
ROSTER = os.getenv("MOCK_ROSTER_URL", "http://localhost:8001")
GATEWAY_RESOURCE = "http://localhost:8000/mcp"
REDIRECT = "http://127.0.0.1:8765/callback"
USERS = {"anna": "dev-only-anna", "erik": "dev-only-erik"}

pytestmark = pytest.mark.integration


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
        yield client


@pytest.fixture
async def server(http: httpx.AsyncClient) -> AuthServerMetadata:
    return await discover_authorization_server(http, ISSUER)


LoginFn = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def login(http: httpx.AsyncClient, server: AuthServerMetadata) -> LoginFn:
    async def _login(user: str, scopes: str = "openid roster.read swap.write") -> dict[str, Any]:
        return await headless_login(
            http,
            server,
            client_id="crewgate-mcp-client",
            redirect_uri=REDIRECT,
            username=user,
            password=USERS[user],
            scopes=scopes.split(),
            resource=GATEWAY_RESOURCE,
        )

    return _login


@pytest.fixture
def gateway_verifier(http: httpx.AsyncClient, server: AuthServerMetadata) -> TokenVerifier:
    assert server.jwks_uri
    return TokenVerifier(ISSUER, "crewgate-gateway", JwksCache(server.jwks_uri, http))


@pytest.fixture
def exchanger(http: httpx.AsyncClient, server: AuthServerMetadata) -> TokenExchanger:
    return TokenExchanger(
        http, server.token_endpoint, "crewgate-gateway", SecretStr("dev-only-gateway-secret")
    )


@pytest.fixture
def principal_for(login: LoginFn, gateway_verifier: TokenVerifier) -> Callable[..., Awaitable[Principal]]:
    async def _p(user: str, scopes: str = "openid roster.read swap.write") -> Principal:
        tokens = await login(user, scopes)
        return await gateway_verifier.verify(tokens["access_token"])

    return _p
