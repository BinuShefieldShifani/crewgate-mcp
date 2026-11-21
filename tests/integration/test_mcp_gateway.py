"""The real gateway, over the wire: MCP auth handshake, raw JSON-RPC tools, human approval.

Needs the full stack (make up): Keycloak, mock crew API (OIDC + HMAC) and the gateway.
"""

import itertools
import json
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jsonschema
import pytest

from crewgate.clientauth import discover_authorization_server, fetch_protected_resource_metadata
from crewgate.clientauth.devlogin import headless_login, submit_login_form

pytestmark = pytest.mark.integration
GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")
PASSWORDS = {"anna": "dev-only-anna", "erik": "dev-only-erik"}
_ids = itertools.count(1)


async def sign_in(
    http: httpx.AsyncClient, user: str, *, scopes: str = "openid roster.read swap.write"
) -> str:
    """The full MCP client discovery: 401 -> PRM -> authorization server -> PKCE sign-in."""
    challenge = (await http.post(f"{GATEWAY}/mcp", json={})).headers["www-authenticate"]
    prm_url = challenge.split('resource_metadata="')[1].split('"')[0]
    prm = await fetch_protected_resource_metadata(http, prm_url)
    server = await discover_authorization_server(http, prm["authorization_servers"][0])
    tokens = await headless_login(
        http,
        server,
        client_id="crewgate-mcp-client",
        redirect_uri="http://127.0.0.1:8765/callback",
        username=user,
        password=PASSWORDS[user],
        scopes=scopes.split(),
        resource=prm["resource"],
    )
    return str(tokens["access_token"])


async def approval_sign_in(http: httpx.AsyncClient, user: str) -> str:
    """What the approval page does in a browser, done headless: fresh sign-in, approval app."""
    issuer = "http://localhost:8080/realms/crewgate"
    server = await discover_authorization_server(http, issuer)
    tokens = await headless_login(
        http,
        server,
        client_id="crewgate-approvals",
        client_secret="dev-only-approvals-secret",  # test only: the agent never has this
        redirect_uri=f"{GATEWAY}/approvals/callback",
        username=user,
        password=PASSWORDS[user],
        scopes=["openid", "swap.write"],
    )
    return str(tokens["access_token"])


async def rpc(http: httpx.AsyncClient, token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    r = await http.post(
        f"{GATEWAY}/mcp",
        json={"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {}},
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


async def call(http: httpx.AsyncClient, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = (await rpc(http, token, "tools/call", {"name": tool, "arguments": args}))[
        "result"
    ]
    return result


@pytest.fixture
async def http() -> Any:
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as client:
        yield client


async def test_mcp_auth_handshake_from_nothing(http: httpx.AsyncClient) -> None:
    r = await http.post(f"{GATEWAY}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in r.headers["www-authenticate"]
    )
    token = await sign_in(http, "anna")
    init = await rpc(
        http,
        token,
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "it", "version": "1"}},
    )
    assert init["result"]["serverInfo"]["name"] == "crewgate"


async def test_read_tools_with_structured_output(http: httpx.AsyncClient) -> None:
    token = await sign_in(http, "anna")
    tools = {t["name"]: t for t in (await rpc(http, token, "tools/list"))["result"]["tools"]}
    roster = await call(http, token, "crew_get_roster", {"crew_id": "C0007"})
    assert roster["isError"] is False
    jsonschema.validate(roster["structuredContent"], tools["crew_get_roster"]["outputSchema"])
    cover = await call(
        http, token, "crew_find_cover", {"date": "2026-12-03", "base": "GOT", "max_candidates": 2}
    )
    assert cover["isError"] is False and cover["structuredContent"]["checked"] >= 1


async def test_write_waits_for_a_human_and_the_agent_cannot_approve_itself(http: httpx.AsyncClient) -> None:
    agent = await sign_in(http, "anna")
    args = {
        "requester_id": "C0003",
        "partner_id": "C0004",
        "date": "2026-12-05",
        "reason": "integration test",
    }
    pending = (await call(http, agent, "crew_propose_swap", args))["structuredContent"]
    assert pending["status"] == "awaiting_approval"
    aid = pending["approval_id"]

    self_approve = await http.post(
        f"{GATEWAY}/approvals/{aid}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {agent}"},
    )
    assert (self_approve.status_code, self_approve.json()["error"]) == (403, "not_a_human_sign_in")

    other = await http.post(
        f"{GATEWAY}/approvals/{aid}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {await approval_sign_in(http, 'erik')}"},
    )
    assert other.json()["error"] == "wrong_user"

    status = await call(http, agent, "gateway_approval_status", {"approval_id": aid})
    assert status["structuredContent"]["status"] == "pending"

    human = await approval_sign_in(http, "anna")
    ok = await http.post(
        f"{GATEWAY}/approvals/{aid}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {human}"},
    )
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    done = (await call(http, agent, "gateway_approval_status", {"approval_id": aid}))["structuredContent"]
    assert done["status"] == "executed" and done["result"]["swap_id"].startswith("S")


async def test_the_real_browser_approval_path(http: httpx.AsyncClient) -> None:
    """Approval link -> Keycloak login page -> callback -> done, hop by hop like a browser."""
    agent = await sign_in(http, "anna")
    args = {"requester_id": "C0005", "partner_id": "C0006", "date": "2026-12-06"}
    pending = (await call(http, agent, "crew_propose_swap", args))["structuredContent"]

    page = await http.get(pending["approve_url"])
    assert page.status_code == 200 and "C0005" in page.text
    start = await http.get(f"{pending['approve_url']}/start", params={"decision": "approve"})
    assert start.status_code == 303
    login_page = await http.get(start.headers["location"])  # Keycloak asks for the password again
    redirect = await submit_login_form(http, login_page, "anna", PASSWORDS["anna"])
    callback = redirect.headers["location"]
    assert callback.startswith(f"{GATEWAY}/approvals/callback?")
    assert "code" in parse_qs(urlsplit(callback).query)
    done = await http.get(callback)
    assert done.status_code == 200 and "Done" in done.text and "executed" in done.text


async def test_viewer_gets_a_clear_refusal_after_approving(http: httpx.AsyncClient) -> None:
    erik = await sign_in(http, "erik")
    args = {"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-07"}
    pending = (await call(http, erik, "crew_propose_swap", args))["structuredContent"]
    human = await approval_sign_in(http, "erik")
    r = await http.post(
        f"{GATEWAY}/approvals/{pending['approval_id']}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {human}"},
    )
    assert (r.status_code, r.json()["error"]) == (502, "partner_forbidden")  # his role still says no


async def test_metrics_show_the_traffic(http: httpx.AsyncClient) -> None:
    body = (await http.get(f"{GATEWAY}/metrics")).text
    assert 'crewgate_approvals_total{outcome="approved",tool="crew.propose_swap"}' in body
    assert "crewgate_partner_requests_total" in body
    assert json.dumps("anna") not in body  # no user names in metric labels
