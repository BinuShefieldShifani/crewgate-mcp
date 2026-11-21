"""Sign-in, token checks and OBO against a real Keycloak. These prove the realm config, not just our code."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest

from crewgate.clientauth import AuthServerMetadata, PkcePair, build_authorization_url
from crewgate.core.auth import AuthError, AuthErrorCode, OnBehalfOf, Principal, TokenExchanger, TokenVerifier
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.plugins.crew.client import RosterClient
from crewgate.plugins.crew.config import CrewSettings

from .conftest import REDIRECT, ROSTER, LoginFn

pytestmark = pytest.mark.integration
ENTITLEMENTS = Path(__file__).resolve().parents[2] / "config" / "entitlements.toml"
PrincipalFor = Callable[..., Awaitable[Principal]]


def claims(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


# ---- sign-in (PKCE) ---------------------------------------------------------------------


async def test_pkce_login_gives_a_gateway_only_token(login: LoginFn) -> None:
    c = claims((await login("anna"))["access_token"])
    assert c["aud"] == "crewgate-gateway"
    assert c["azp"] == "crewgate-mcp-client"
    assert {"roster.read", "swap.write"} <= set(str(c["scope"]).split())
    # data minimisation: no names, no email, no roles in the token the MCP client holds
    for claim in ("email", "name", "given_name", "family_name", "resource_access", "realm_access"):
        assert claim not in c


async def test_login_without_pkce_is_refused(http: httpx.AsyncClient, server: AuthServerMetadata) -> None:
    r = await http.get(
        server.authorization_endpoint,
        params={"client_id": "crewgate-mcp-client", "response_type": "code", "redirect_uri": REDIRECT},
    )
    error = parse_qs(urlsplit(r.headers.get("location", "")).query).get("error")
    assert r.status_code in (302, 303) and error == ["invalid_request"]


async def test_plain_pkce_is_refused(http: httpx.AsyncClient, server: AuthServerMetadata) -> None:
    pkce = PkcePair(verifier="v" * 50, challenge="v" * 50, method="plain")
    url = build_authorization_url(
        server.authorization_endpoint,
        client_id="crewgate-mcp-client",
        redirect_uri=REDIRECT,
        scopes=["openid"],
        state="s",
        pkce=pkce,
    )
    r = await http.get(url)
    assert parse_qs(urlsplit(r.headers.get("location", "")).query).get("error") == ["invalid_request"]


async def test_unregistered_redirect_is_refused(http: httpx.AsyncClient, server: AuthServerMetadata) -> None:
    url = build_authorization_url(
        server.authorization_endpoint,
        client_id="crewgate-mcp-client",
        redirect_uri="https://evil.example/callback",
        scopes=["openid"],
        state="s",
        pkce=PkcePair.generate(),
    )
    r = await http.get(url)
    assert r.status_code == 400  # Keycloak shows an error page instead of redirecting anywhere


# ---- gateway token checks ----------------------------------------------------------------


async def test_gateway_verifies_real_keycloak_tokens(principal_for: PrincipalFor) -> None:
    anna = await principal_for("anna")
    assert anna.username == "anna"
    assert anna.client_id == "crewgate-mcp-client"
    assert anna.scopes >= {"roster.read", "swap.write"}


async def test_gateway_rejects_partner_tokens(
    principal_for: PrincipalFor, exchanger: TokenExchanger, gateway_verifier: TokenVerifier
) -> None:
    partner_token = await exchanger.exchange(await principal_for("anna"), "crew-roster-api", ["roster.read"])
    with pytest.raises(AuthError) as err:
        await gateway_verifier.verify(partner_token)
    assert "not issued for this resource" in err.value.description


# ---- OBO token exchange ------------------------------------------------------------------


async def test_obo_token_keeps_the_user_and_gets_the_partner_audience(
    principal_for: PrincipalFor, exchanger: TokenExchanger
) -> None:
    anna = await principal_for("anna")
    c = claims(await exchanger.exchange(anna, "crew-roster-api", ["roster.read", "swap.write"]))
    assert c["sub"] == anna.subject
    assert c["aud"] == "crew-roster-api"
    assert c["azp"] == "crewgate-gateway"
    assert c["resource_access"] == {"crew-roster-api": {"roles": ["roster-reader", "swap-requester"]}}


async def test_keycloak_alone_would_upscope(
    principal_for: PrincipalFor, http: httpx.AsyncClient, server: AuthServerMetadata
) -> None:
    """Characterisation test: documents why TokenExchanger refuses to up-scope.

    If this ever fails, Keycloak started enforcing it itself. Keep our check anyway.
    """
    anna = await principal_for("anna", "openid roster.read")
    r = await http.post(
        server.token_endpoint,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": anna.token.get_secret_value(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": "crew-roster-api",
            "scope": "roster.read swap.write",
        },
        auth=("crewgate-gateway", "dev-only-gateway-secret"),
    )
    assert r.status_code == 200
    assert "swap.write" in str(claims(r.json()["access_token"])["scope"]).split()


async def test_gateway_refuses_to_upscope(principal_for: PrincipalFor, exchanger: TokenExchanger) -> None:
    anna = await principal_for("anna", "openid roster.read")
    with pytest.raises(AuthError) as err:
        await exchanger.exchange(anna, "crew-roster-api", ["roster.read", "swap.write"])
    assert err.value.code is AuthErrorCode.INSUFFICIENT_SCOPE
    assert exchanger.exchange_count == 0


async def test_exchange_needs_the_gateway_secret(
    principal_for: PrincipalFor, http: httpx.AsyncClient, server: AuthServerMetadata
) -> None:
    from pydantic import SecretStr

    wrong = TokenExchanger(http, server.token_endpoint, "crewgate-gateway", SecretStr("wrong"))
    with pytest.raises(PartnerError) as err:
        await wrong.exchange(await principal_for("anna"), "crew-roster-api", ["roster.read"])
    assert err.value.category is ErrorCategory.AUTH


# ---- end to end through the crew plugin to the partner API -------------------------------


def roster(exchanger: TokenExchanger, p: Principal, scopes: list[str]) -> RosterClient:
    return RosterClient(
        CrewSettings(api_base_url=ROSTER), auth=OnBehalfOf(exchanger, p, "crew-roster-api", scopes)
    )


async def test_planner_can_read_and_swap(principal_for: PrincipalFor, exchanger: TokenExchanger) -> None:
    anna = await principal_for("anna")
    async with roster(exchanger, anna, ["roster.read", "swap.write"]) as client:
        crew = [c async for c in client.iter_crew(page_size=50)]
        swap = await client.request_swap(crew[0]["crew_id"], crew[1]["crew_id"], "2026-12-03")
    assert len(crew) == 120
    assert swap["status"] == "pending_approval"
    assert swap["requested_by"] == anna.subject
    assert exchanger.exchange_count == 1  # one exchange, reused for all four calls


async def test_viewer_cannot_swap_even_with_the_scope(
    principal_for: PrincipalFor, exchanger: TokenExchanger
) -> None:
    erik = await principal_for("erik")
    assert "swap.write" in erik.scopes  # his app was allowed to ask for it...
    async with roster(exchanger, erik, ["roster.read", "swap.write"]) as client:
        await client.get_roster("C0001")
        with pytest.raises(PartnerError) as err:
            await client.request_swap("C0001", "C0002", "2026-12-03")
    assert err.value.category is ErrorCategory.FORBIDDEN  # ...but his role says no
    assert "insufficient_role" in err.value.message


async def test_partner_rejects_passthrough_of_the_users_token(
    login: LoginFn, http: httpx.AsyncClient
) -> None:
    user_token = (await login("anna"))["access_token"]
    r = await http.get(f"{ROSTER}/crew", headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 401


# ---- signed partner traffic and the full guard (Step 3) ----------------------------------


async def test_partner_refuses_unsigned_calls_even_with_a_valid_obo_token(
    principal_for: PrincipalFor, exchanger: TokenExchanger, http: httpx.AsyncClient
) -> None:
    obo = await exchanger.exchange(await principal_for("anna"), "crew-roster-api", ["roster.read"])
    r = await http.get(f"{ROSTER}/crew", headers={"Authorization": f"Bearer {obo}"})
    assert r.status_code == 401 and r.json()["detail"] == "missing_signature"


async def test_guarded_tool_calls_with_real_users(
    principal_for: PrincipalFor, exchanger: TokenExchanger
) -> None:
    from typing import Any

    from pydantic import SecretStr

    from crewgate.core.audit import AuditLog, MemorySink, verify_chain
    from crewgate.core.entitlements import EntitlementPolicy
    from crewgate.core.guard import GuardDenied, ToolGuard
    from crewgate.core.metrics import GatewayMetrics
    from crewgate.core.plugin import ToolSpec
    from crewgate.core.ratelimit import RateLimiter

    async def get_roster(p: Principal, crew_id: str) -> Any:
        async with roster(exchanger, p, ["roster.read"]) as client:
            return await client.get_roster(crew_id)

    async def propose_swap(p: Principal, requester_id: str, partner_id: str, day: str) -> Any:
        async with roster(exchanger, p, ["roster.read", "swap.write"]) as client:
            return await client.request_swap(requester_id, partner_id, day)

    read = ToolSpec("crew.get_roster", "Read one roster", "roster.read", get_roster)
    # Called straight through guard.run to show its checks. The real crew.propose_swap is a write
    # tool: through the MCP server it needs human approval first (see make demo-mcp).
    swap = ToolSpec("crew.propose_swap", "Propose a swap", "swap.write", propose_swap)
    key = SecretStr("dev-only-audit-key")
    sink, metrics = MemorySink(), GatewayMetrics()
    policy = EntitlementPolicy.from_toml(ENTITLEMENTS)
    guard = ToolGuard(policy, RateLimiter(), AuditLog(key, sink), metrics)

    anna = await principal_for("anna")
    assert (await guard.run(anna, read, {"crew_id": "C0001"}))["crew_id"] == "C0001"
    assert (
        await guard.run(anna, swap, {"requester_id": "C0001", "partner_id": "C0002", "day": "2026-12-03"})
    )["status"] == "pending_approval"

    reader = await principal_for("anna", "openid roster.read")  # signed in without swap.write
    with pytest.raises(GuardDenied) as denied:
        await guard.run(reader, swap, {"requester_id": "C0001", "partner_id": "C0002", "day": "2026-12-03"})
    assert denied.value.reason == "insufficient_scope"

    erik = await principal_for("erik")  # has the scope, lacks the role: the partner says no
    with pytest.raises(PartnerError):
        await guard.run(erik, swap, {"requester_id": "C0001", "partner_id": "C0002", "day": "2026-12-03"})

    outcomes = [line.split('"outcome":"')[1].split('"')[0] for line in sink.lines]
    assert outcomes == ["ok", "ok", "-", "error:forbidden"]
    assert verify_chain(sink.lines, key).ok
    assert "C0001" not in "".join(sink.lines)  # arguments are digested, not stored
