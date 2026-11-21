"""Step 4 demo: talk to the crewgate MCP server the way any MCP client would, in raw JSON-RPC.

  1. no token -> 401 pointing at the Protected Resource Metadata
  2. discover the identity provider from it, sign in with PKCE
  3. initialize, tools/list, read tools with structured output
  4. propose a swap -> awaiting approval, nothing has happened
  5. the agent tries to approve itself -> refused
  6. the person signs in again in the approval app and approves -> done

Start the stack first:  make up
Then run:               make demo-mcp
"""

import asyncio
import itertools
import json
from typing import Any

import httpx

from crewgate.clientauth import discover_authorization_server, fetch_protected_resource_metadata
from crewgate.clientauth.devlogin import headless_login

GATEWAY = "http://localhost:8000"
_ids = itertools.count(1)


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
    r.raise_for_status()
    return r.json()


async def tool(http: httpx.AsyncClient, token: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = (await rpc(http, token, "tools/call", {"name": name, "arguments": args}))[
        "result"
    ]
    return result


async def main() -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as http:
        r = await http.post(f"{GATEWAY}/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"})
        challenge = r.headers["www-authenticate"]
        print(f"1. No token: HTTP {r.status_code}\n   WWW-Authenticate: {challenge}")

        prm = await fetch_protected_resource_metadata(
            http, challenge.split('resource_metadata="')[1].split('"')[0]
        )
        server = await discover_authorization_server(http, prm["authorization_servers"][0])
        print(f"2. Metadata says: sign in at {server.issuer} (scopes {', '.join(prm['scopes_supported'])})")
        login = await headless_login(
            http,
            server,
            client_id="crewgate-mcp-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            username="anna",
            password="dev-only-anna",
            scopes=["openid", "roster.read", "swap.write"],
            resource=prm["resource"],
        )
        agent = str(login["access_token"])
        print("   Signed in as anna with authorization code + PKCE")

        init = await rpc(
            http,
            agent,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "demo", "version": "1"},
            },
        )
        tools = [t["name"] for t in (await rpc(http, agent, "tools/list"))["result"]["tools"]]
        print(f"3. initialize -> {init['result']['serverInfo']['name']}; tools/list -> {', '.join(tools)}")
        roster = await tool(http, agent, "crew_get_roster", {"crew_id": "C0001", "to_date": "2026-12-03"})
        days = ", ".join(f"{d['date'][5:]} {d['duty']}" for d in roster["structuredContent"]["days"])
        print(f"   crew_get_roster C0001: {days}")
        cover = await tool(
            http, agent, "crew_find_cover", {"date": "2026-12-03", "base": "GOT", "max_candidates": 3}
        )
        names = ", ".join(
            f"{c['crew_id']} ({c['duty_that_day']})" for c in cover["structuredContent"]["candidates"]
        )
        print(f"   crew_find_cover GOT 03 Dec: {names}")
        bad = await tool(http, agent, "crew_get_roster", {"crew_id": "42"})
        print(
            f"   bad argument -> isError={bad['isError']}: {json.loads(bad['content'][0]['text'])['error']}"
        )

        swap = {"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-03", "reason": "sick leave"}
        pending = (await tool(http, agent, "crew_propose_swap", swap))["structuredContent"]
        print(
            f"4. crew_propose_swap -> {pending['status']}: {pending['summary']}\n   {pending['approve_url']}"
        )

        r = await http.post(
            f"{GATEWAY}/approvals/{pending['approval_id']}/decision",
            json={"decision": "approve"},
            headers={"authorization": f"Bearer {agent}"},
        )
        print(f"5. Agent approves with its own token -> HTTP {r.status_code} {r.json()['error']}")

        human = await headless_login(
            http,
            server,
            client_id="crewgate-approvals",
            client_secret="dev-only-approvals-secret",  # held by the gateway, never by the agent
            redirect_uri=f"{GATEWAY}/approvals/callback",
            username="anna",
            password="dev-only-anna",
            scopes=["openid", "swap.write"],
        )
        r = await http.post(
            f"{GATEWAY}/approvals/{pending['approval_id']}/decision",
            json={"decision": "approve"},
            headers={"authorization": f"Bearer {human['access_token']}"},
        )
        print(f"6. Anna signs in again in the approval app and approves -> {r.json()['status']}")
        status = await tool(http, agent, "gateway_approval_status", {"approval_id": pending["approval_id"]})
        done = status["structuredContent"]
        print(f"   gateway_approval_status -> {done['status']}, swap {done['result']['swap_id']}")


if __name__ == "__main__":
    asyncio.run(main())
