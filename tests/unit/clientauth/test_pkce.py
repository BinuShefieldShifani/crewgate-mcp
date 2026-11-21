"""Client sign-in pieces: PKCE maths, discovery rules from the MCP spec, token requests."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from crewgate.clientauth import (
    DiscoveryError,
    PkcePair,
    TokenRequestError,
    build_authorization_url,
    discover_authorization_server,
    discovery_urls,
    exchange_code,
    fetch_protected_resource_metadata,
)

ISSUER = "https://idp.test/realms/crewgate"


def test_rfc7636_appendix_b_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert PkcePair.challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pairs_are_valid_and_unique() -> None:
    a, b = PkcePair.generate(), PkcePair.generate()
    assert 43 <= len(a.verifier) <= 128
    assert a.challenge == PkcePair.challenge_for(a.verifier)
    assert a.method == "S256"
    assert a.verifier != b.verifier


@pytest.mark.parametrize("length", [42, 129])
def test_verifier_length_limits(length: int) -> None:
    with pytest.raises(ValueError, match="43 to 128"):
        PkcePair.challenge_for("a" * length)


def test_authorization_url_has_everything_the_spec_needs() -> None:
    pkce = PkcePair.generate()
    url = build_authorization_url(
        f"{ISSUER}/protocol/openid-connect/auth",
        client_id="crewgate-mcp-client",
        redirect_uri="http://127.0.0.1:8765/callback",
        scopes=["openid", "roster.read"],
        state="s1",
        pkce=pkce,
        resource="http://localhost:8000/mcp",
    )
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q == {
        "response_type": "code",
        "client_id": "crewgate-mcp-client",
        "redirect_uri": "http://127.0.0.1:8765/callback",
        "scope": "openid roster.read",
        "state": "s1",
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "resource": "http://localhost:8000/mcp",
    }
    extra = build_authorization_url(
        "https://a/auth?x=1", client_id="c", redirect_uri="r", scopes=[], state="s", pkce=pkce
    )
    assert extra.startswith("https://a/auth?x=1&") and "resource" not in extra


def test_discovery_order() -> None:
    assert discovery_urls(ISSUER) == [
        "https://idp.test/.well-known/oauth-authorization-server/realms/crewgate",
        "https://idp.test/.well-known/openid-configuration/realms/crewgate",
        "https://idp.test/realms/crewgate/.well-known/openid-configuration",
    ]
    assert discovery_urls("https://idp.test") == [
        "https://idp.test/.well-known/oauth-authorization-server",
        "https://idp.test/.well-known/openid-configuration",
    ]


def _server(docs: dict[str, dict[str, object]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        doc = docs.get(str(request.url))
        return httpx.Response(200, json=doc) if doc is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _doc(**over: object) -> dict[str, object]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/certs",
        "code_challenge_methods_supported": ["plain", "S256"],
        **over,
    }


async def test_discovery_falls_through_to_oidc() -> None:
    http = _server({f"{ISSUER}/.well-known/openid-configuration": _doc()})
    meta = await discover_authorization_server(http, ISSUER)
    assert meta.token_endpoint == f"{ISSUER}/token"
    assert "S256" in meta.code_challenge_methods_supported


async def test_discovery_refuses_issuer_mix_up() -> None:
    http = _server({discovery_urls(ISSUER)[0]: _doc(issuer="https://evil.test")})
    with pytest.raises(DiscoveryError, match="issuer mismatch"):
        await discover_authorization_server(http, ISSUER)


@pytest.mark.parametrize("methods", [["plain"], None])
async def test_discovery_refuses_servers_without_s256(methods: object) -> None:
    http = _server({discovery_urls(ISSUER)[0]: _doc(code_challenge_methods_supported=methods)})
    with pytest.raises(DiscoveryError, match="S256"):
        await discover_authorization_server(http, ISSUER)


async def test_discovery_with_nothing_published() -> None:
    with pytest.raises(DiscoveryError, match="no authorization server"):
        await discover_authorization_server(_server({}), ISSUER)


async def test_protected_resource_metadata() -> None:
    url = "https://gw/.well-known/oauth-protected-resource/mcp"
    good = _server({url: {"resource": "https://gw/mcp", "authorization_servers": [ISSUER]}})
    assert (await fetch_protected_resource_metadata(good, url))["authorization_servers"] == [ISSUER]
    bad = _server({url: {"resource": "https://gw/mcp", "authorization_servers": []}})
    with pytest.raises(DiscoveryError):
        await fetch_protected_resource_metadata(bad, url)


async def test_exchange_code_sends_verifier_and_resource() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"access_token": "at", "expires_in": 300})

    pkce = PkcePair.generate()
    body = await exchange_code(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        "https://idp/token",
        client_id="c",
        code="abc",
        redirect_uri="r",
        pkce=pkce,
        resource="http://localhost:8000/mcp",
    )
    assert body["access_token"] == "at"
    assert seen["code_verifier"] == pkce.verifier
    assert seen["resource"] == "http://localhost:8000/mcp"
    assert seen["grant_type"] == "authorization_code"


@pytest.mark.parametrize(
    ("body", "error"), [({"error": "invalid_grant"}, "invalid_grant"), (None, "unknown_error")]
)
async def test_exchange_code_errors(body: object, error: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=body) if body else httpx.Response(400, text="nope")

    with pytest.raises(TokenRequestError) as err:
        await exchange_code(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            "https://idp/token",
            client_id="c",
            code="x",
            redirect_uri="r",
            pkce=PkcePair.generate(),
        )
    assert err.value.error == error
    assert err.value.status == 400
