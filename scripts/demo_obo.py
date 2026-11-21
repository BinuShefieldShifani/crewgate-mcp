"""Step 2 demo: PKCE sign-in, token check, OBO exchange, and the partner enforcing roles.

Start the stack first:  make up
Then run:               make demo
"""

import asyncio

import httpx
import jwt
from pydantic import SecretStr

from crewgate.clientauth import discover_authorization_server
from crewgate.clientauth.devlogin import headless_login
from crewgate.core.auth import AuthError, JwksCache, OnBehalfOf, TokenExchanger, TokenVerifier
from crewgate.core.errors import PartnerError
from crewgate.plugins.crew.client import RosterClient
from crewgate.plugins.crew.config import CrewSettings

ISSUER = "http://localhost:8080/realms/crewgate"
USERS = {"anna": "dev-only-anna", "erik": "dev-only-erik"}


async def main() -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as http:
        server = await discover_authorization_server(http, ISSUER)
        if not server.jwks_uri:
            raise SystemExit("authorization server publishes no jwks_uri")
        print(
            f"Discovered {server.issuer} (PKCE methods: {', '.join(server.code_challenge_methods_supported)})"
        )
        verifier = TokenVerifier(ISSUER, "crewgate-gateway", JwksCache(server.jwks_uri, http))
        exchanger = TokenExchanger(
            http, server.token_endpoint, "crewgate-gateway", SecretStr("dev-only-gateway-secret")
        )

        for user in ("anna", "erik"):
            tokens = await headless_login(
                http,
                server,
                client_id="crewgate-mcp-client",
                redirect_uri="http://127.0.0.1:8765/callback",
                username=user,
                password=USERS[user],
                scopes=["openid", "roster.read", "swap.write"],
                resource="http://localhost:8000/mcp",
            )
            who = await verifier.verify(tokens["access_token"])
            print(f"\n{user}: signed in with PKCE, gateway accepted token (aud=crewgate-gateway)")
            auth = OnBehalfOf(exchanger, who, "crew-roster-api", ["roster.read", "swap.write"])
            obo_token = await exchanger.exchange(who, "crew-roster-api", auth.scopes)
            obo = jwt.decode(obo_token, options={"verify_signature": False})
            roles = obo["resource_access"]["crew-roster-api"]["roles"]
            print(f"  OBO token: aud={obo['aud']} sub=same user, roles={roles}")
            async with RosterClient(CrewSettings(api_base_url="http://localhost:8001"), auth=auth) as client:
                crew = [c async for c in client.iter_crew(page_size=50)]
                print(f"  read {len(crew)} crew members")
                try:
                    swap = await client.request_swap("C0001", "C0002", "2026-12-03")
                    print(f"  swap {swap['swap_id']}: {swap['status']}")
                except PartnerError as exc:
                    print(f"  swap refused by partner: {exc.message}")

        print("\nanna signs in with roster.read only, gateway is asked for swap.write:")
        tokens = await headless_login(
            http,
            server,
            client_id="crewgate-mcp-client",
            redirect_uri="http://127.0.0.1:8765/callback",
            username="anna",
            password=USERS["anna"],
            scopes=["openid", "roster.read"],
        )
        reader = await verifier.verify(tokens["access_token"])
        try:
            await exchanger.exchange(reader, "crew-roster-api", ["roster.read", "swap.write"])
        except AuthError as exc:
            print(f"  refused before calling Keycloak: {exc.description}")
        print(f"\nToken exchanges made: {exchanger.exchange_count}")


if __name__ == "__main__":
    asyncio.run(main())
