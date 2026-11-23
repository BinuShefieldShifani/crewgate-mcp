"""Chat with the crewgate agent against the running stack (make up), with a real model.

    GITHUB_TOKEN=<a token with models:read> uv run python scripts/agent_chat.py
    uv run python scripts/agent_chat.py --once "Find flights from ARN to LHR on 2026-12-20"

Signs in as the dev-only test user anna (PKCE, real Keycloak), then every message goes through
the LangGraph agent and the MCP gateway. Write tools print an approval link: open it in your
browser and sign in as anna to approve. Each reply shows whether the guard changed it.
"""

import argparse
import asyncio

import httpx

from crewgate.agent import Agent, AgentSettings, GatewayTools, LLMError
from crewgate.clientauth import discover_authorization_server
from crewgate.clientauth.devlogin import headless_login

ISSUER = "http://localhost:8080/realms/crewgate"
SCOPES = ["openid", "roster.read", "swap.write", "booking.read", "booking.write"]


async def sign_in() -> str:
    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as http:
        server = await discover_authorization_server(http, ISSUER)
        token = await headless_login(
            http,
            server,
            client_id="crewgate-mcp-client",
            client_secret=None,
            username="anna",
            password="dev-only-anna",  # the realm's dev-only test user
            redirect_uri="http://127.0.0.1:8765/callback",
            scopes=SCOPES,
        )
        return str(token["access_token"])


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", help="send one message and exit")
    a = p.parse_args()
    settings = AgentSettings()
    try:
        model = settings.chat_model()
    except LLMError as exc:
        raise SystemExit(f"{exc}. A GitHub token with models:read works: export GITHUB_TOKEN=...") from None
    token = await sign_in()
    try:
        async with GatewayTools.connect(
            settings.gateway_url, token, max_chars=settings.max_tool_result_chars
        ) as tools:
            agent = Agent(model, tools, max_steps=settings.max_steps, market=settings.default_market)
            print(f"connected: {len(tools.tools)} tools, model {model.name}. Empty line to quit.")
            while True:
                text = a.once or (await asyncio.to_thread(input, "\nyou> ")).strip()
                if not text:
                    break
                turn = await agent.ask(text)
                print(f"\nagent> {turn.reply}")
                note = "" if turn.guard.ok else f"  [guard replaced the draft: {turn.guard.draft!r}]"
                print(f"  tools: {', '.join(turn.tool_calls) or '-'}  tokens: {turn.tokens}{note}")
                if a.once:
                    break
    finally:
        await model.aclose()


if __name__ == "__main__":
    asyncio.run(main())
