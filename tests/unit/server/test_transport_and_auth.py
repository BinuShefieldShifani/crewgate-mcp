"""HTTP layer: the MCP auth handshake, metadata, host checks, health and metrics."""

from tests.helpers.idp import PARTNER

from .conftest import BASE, Env

PRM_URL = f"{BASE}/.well-known/oauth-protected-resource/mcp"


async def test_no_token_gets_401_pointing_at_metadata(env: Env) -> None:
    r = await env.rpc("tools/list", token=None)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ") and f'resource_metadata="{PRM_URL}"' in challenge
    assert r.json() == {"error": "invalid_token", "error_description": "authentication required"}


async def test_bad_tokens_say_why(env: Env) -> None:
    cases = {
        env.agent_token(exp_in=-300): "token expired",
        env.agent_token(aud=PARTNER): "token was not issued for this resource",
        "garbage": "malformed token",
    }
    for token, why in cases.items():
        r = await env.rpc("tools/list", token=token)
        assert r.status_code == 401
        assert r.json()["error_description"] == why
        assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert env.gw.metrics.value("crewgate_auth_failures_total", reason="token expired") == 1


async def test_protected_resource_metadata(env: Env) -> None:
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        doc = (await env.http.get(path)).json()
        assert doc["resource"] == f"{BASE}/mcp"
        assert doc["authorization_servers"] == ["https://idp.test/realms/crewgate"]
        assert doc["scopes_supported"] == ["openid", "roster.read", "swap.write"]
        assert doc["bearer_methods_supported"] == ["header"]


async def test_dns_rebinding_protection(env: Env) -> None:
    r = await env.http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "host": "evil.example",
            "authorization": f"Bearer {env.agent_token()}",
            "accept": "application/json, text/event-stream",
        },
    )
    assert r.status_code == 421


async def test_health_and_metrics(env: Env) -> None:
    assert (await env.http.get("/healthz")).json() == {"status": "ok"}
    await env.rpc("tools/list", token=None)
    body = (await env.http.get("/metrics")).text
    assert "crewgate_tool_calls_total" in body and "crewgate_auth_failures_total" in body


async def test_unknown_method_is_a_jsonrpc_error(env: Env) -> None:
    r = await env.rpc("does/not/exist")
    assert r.json()["error"]["code"] == -32601


async def test_initialize(env: Env) -> None:
    r = await env.rpc(
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}},
    )
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "crewgate"
    assert "awaiting approval" in result["instructions"]
    assert "tools" in result["capabilities"]
