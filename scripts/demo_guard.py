"""Step 3 demo: the guard around every tool call, against the real stack.

Shows entitlements, scopes, rate limits, signed partner calls, redacted logs,
the audit chain (and what tampering looks like) and Prometheus metrics.

Start the stack first:  make up
Then run:               make demo-guard
Check the audit file afterwards with:  make audit-verify
"""

import asyncio
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from crewgate.clientauth import discover_authorization_server
from crewgate.clientauth.devlogin import headless_login
from crewgate.core.audit import AuditLog, FileSink, verify_chain
from crewgate.core.auth import JwksCache, OnBehalfOf, Principal, TokenExchanger, TokenVerifier
from crewgate.core.entitlements import EntitlementPolicy
from crewgate.core.errors import PartnerError
from crewgate.core.guard import GuardDenied, ToolGuard
from crewgate.core.metrics import GatewayMetrics
from crewgate.core.plugin import ToolSpec
from crewgate.core.ratelimit import RateLimiter
from crewgate.core.safe_logging import configure_logging
from crewgate.plugins.crew.client import RosterClient
from crewgate.plugins.crew.config import CrewSettings

ISSUER = "http://localhost:8080/realms/crewgate"
AUDIT_FILE = Path(__file__).resolve().parents[1] / "var" / "audit.jsonl"  # git-ignored
AUDIT_KEY = SecretStr("dev-only-audit-key")
REPO = Path(__file__).resolve().parents[1]


async def main() -> None:
    configure_logging(SecretStr("dev-only-pseudonym-key"), level="WARNING")
    log = logging.getLogger("crewgate.demo")
    await asyncio.to_thread(AUDIT_FILE.unlink, missing_ok=True)

    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as http:
        server = await discover_authorization_server(http, ISSUER)
        if not server.jwks_uri:
            raise SystemExit("no jwks_uri")
        verifier = TokenVerifier(ISSUER, "crewgate-gateway", JwksCache(server.jwks_uri, http))
        metrics = GatewayMetrics()
        exchanger = TokenExchanger(
            http,
            server.token_endpoint,
            "crewgate-gateway",
            SecretStr("dev-only-gateway-secret"),
            metrics=metrics,
        )

        async def sign_in(user: str, scopes: str) -> Principal:
            tokens = await headless_login(
                http,
                server,
                client_id="crewgate-mcp-client",
                redirect_uri="http://127.0.0.1:8765/callback",
                username=user,
                password=f"dev-only-{user}",
                scopes=scopes.split(),
            )
            return await verifier.verify(tokens["access_token"])

        def roster(p: Principal, scopes: list[str]) -> RosterClient:
            auth = OnBehalfOf(exchanger, p, "crew-roster-api", scopes)
            return RosterClient(
                CrewSettings(api_base_url="http://localhost:8001"), auth=auth, metrics=metrics
            )

        async def get_roster(p: Principal, crew_id: str) -> Any:
            async with roster(p, ["roster.read"]) as c:
                return await c.get_roster(crew_id)

        async def propose_swap(p: Principal, requester_id: str, partner_id: str, day: str) -> Any:
            async with roster(p, ["roster.read", "swap.write"]) as c:
                return await c.request_swap(requester_id, partner_id, day)

        read = ToolSpec("crew.get_roster", "Read a roster", "roster.read", get_roster)
        # Called straight through guard.run to show its checks. The real crew.propose_swap is a
        # write tool: through the MCP server it needs human approval first (see make demo-mcp).
        swap = ToolSpec("crew.propose_swap", "Propose a swap", "swap.write", propose_swap)
        guard = ToolGuard(
            EntitlementPolicy.from_toml(REPO / "config" / "entitlements.toml"),
            RateLimiter(),
            AuditLog(AUDIT_KEY, FileSink(AUDIT_FILE)),
            metrics,
        )
        swap_args = {"requester_id": "C0001", "partner_id": "C0002", "day": "2026-12-03"}

        async def attempt(label: str, p: Principal, spec: ToolSpec, args: dict[str, Any]) -> None:
            try:
                await guard.run(p, spec, args)
                print(f"  {label:<52} allowed")
            except GuardDenied as exc:
                print(f"  {label:<52} refused by gateway: {exc.reason}")
            except PartnerError as exc:
                print(f"  {label:<52} refused by partner: {exc.message}")

        anna = await sign_in("anna", "openid roster.read swap.write")
        anna_reader = await sign_in("anna", "openid roster.read")
        erik = await sign_in("erik", "openid roster.read swap.write")
        anna_via_booking_app = dataclasses.replace(anna, client_id="booking-assistant-se")

        print("Guarded tool calls (every call is OBO + HMAC-signed to the partner):")
        await attempt("anna reads a roster", anna, read, {"crew_id": "C0001"})
        await attempt("anna proposes a swap", anna, swap, swap_args)
        await attempt("anna, signed in read-only, proposes a swap", anna_reader, swap, swap_args)
        await attempt("erik (viewer) proposes a swap", erik, swap, swap_args)
        await attempt(
            "anna through the booking-only app reads a roster",
            anna_via_booking_app,
            read,
            {"crew_id": "C0001"},
        )

        print("\nRate limit (30 per user per minute for this app):")
        allowed = refused = 0
        for i in range(35):
            try:
                await guard.run(anna, read, {"crew_id": f"C{i + 1:04d}"})
                allowed += 1
            except GuardDenied as exc:
                refused += 1
                retry_after = exc.headers.get("Retry-After")
        print(f"  35 quick reads by anna: {allowed} allowed, {refused} refused, Retry-After={retry_after}s")

        print("\nA log line that mentions personal data and a token:")
        log.warning(
            "swap request for C0042 by anna@crew.example", extra={"subject_token": "eyJhbGciOi.eyJzdWIi.sig"}
        )

        lines = (await asyncio.to_thread(AUDIT_FILE.read_text)).splitlines()
        print(f"\nAudit chain ({AUDIT_FILE.name}): {verify_chain(lines, AUDIT_KEY)}")
        # The realistic attack: rewrite a refusal as if it had been allowed.
        i = next(n for n, line in enumerate(lines) if json.loads(line)["decision"] == "denied")
        record = json.loads(lines[i])
        record["decision"], record["reason"] = "allowed", "ok"
        tampered = [*lines[:i], json.dumps(record), *lines[i + 1 :]]
        print(f"After turning refusal #{record['seq']} into 'allowed': {verify_chain(tampered, AUDIT_KEY)}")

        print("\nMetrics (excerpt of /metrics):")
        for line in metrics.exposition().decode().splitlines():
            if line.startswith(
                (
                    "crewgate_tool_calls_total",
                    "crewgate_denials_total",
                    "crewgate_rate_limited_total",
                    "crewgate_partner_requests_total",
                    "crewgate_token_exchanges_total",
                )
            ):
                print(f"  {line}")


if __name__ == "__main__":
    asyncio.run(main())
