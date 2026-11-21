"""OBO exchange: never up-scope, cache safely, map every IdP failure, and work end to end."""

import time
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from crewgate.core.auth import AuthError, AuthErrorCode, JwksCache, OnBehalfOf, TokenExchanger, TokenVerifier
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.plugins.crew.client import RosterClient
from crewgate.plugins.crew.config import CrewSettings
from mock_roster_api.app import create_app
from mock_roster_api.settings import ApiSettings
from tests.helpers.idp import GATEWAY, ISSUER, JWKS_URL, PARTNER, TOKEN_URL, FakeIdp

ANNA = {"roles": {PARTNER: ["roster-reader", "swap-requester"]}}
ERIK = {"roles": {PARTNER: ["roster-reader"]}}


class Clock:
    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp(users={"user-anna": ANNA, "user-erik": ERIK})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def exchanger(idp: FakeIdp, clock: Clock) -> TokenExchanger:
    return TokenExchanger(idp.client(), TOKEN_URL, GATEWAY, SecretStr("dev-only-gateway-secret"), clock=clock)


async def principal(idp: FakeIdp, **kw: object):  # type: ignore[no-untyped-def]
    v = TokenVerifier(ISSUER, GATEWAY, JwksCache(JWKS_URL, idp.client()))
    return await v.verify(idp.token(**kw))  # type: ignore[arg-type]


async def test_exchange_sends_rfc8693_request(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    p = await principal(idp)
    await exchanger.exchange(p, PARTNER, ["roster.read"])
    form = idp.exchange_calls[0]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert form["subject_token"] == p.token.get_secret_value()
    assert form["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert form["audience"] == PARTNER
    assert form["scope"] == "roster.read"
    assert "client_secret" not in form  # sent as HTTP Basic auth, not in the body


async def test_never_upscopes_and_never_calls_the_idp(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    p = await principal(idp, scope="openid roster.read")
    with pytest.raises(AuthError) as err:
        await exchanger.exchange(p, PARTNER, ["roster.read", "swap.write"])
    assert err.value.code is AuthErrorCode.INSUFFICIENT_SCOPE
    assert "swap.write" in err.value.description
    assert idp.exchange_calls == []


async def test_cached_until_refresh_margin(idp: FakeIdp, exchanger: TokenExchanger, clock: Clock) -> None:
    p = await principal(idp)
    first = await exchanger.exchange(p, PARTNER, ["roster.read"])
    assert await exchanger.exchange(p, PARTNER, ["roster.read"]) == first
    assert exchanger.exchange_count == 1
    clock.t += 300 - 29  # inside the 30 s margin
    await exchanger.exchange(p, PARTNER, ["roster.read"])
    assert exchanger.exchange_count == 2


async def test_cache_is_per_user_audience_and_scope(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    anna, erik = await principal(idp), await principal(idp, sub="user-erik")
    await exchanger.exchange(anna, PARTNER, ["roster.read"])
    await exchanger.exchange(erik, PARTNER, ["roster.read"])
    await exchanger.exchange(anna, PARTNER, ["roster.read", "swap.write"])
    assert exchanger.exchange_count == 3


async def test_partner_token_never_outlives_user_token(
    idp: FakeIdp, exchanger: TokenExchanger, clock: Clock
) -> None:
    idp.partner_ttl = 3600
    p = await principal(idp, exp_in=120)
    await exchanger.exchange(p, PARTNER, ["roster.read"])
    clock.t += 95  # user token expires in 25 s, inside the margin: must re-exchange
    await exchanger.exchange(p, PARTNER, ["roster.read"])
    assert exchanger.exchange_count == 2


async def test_force_skips_cache(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    p = await principal(idp)
    await exchanger.exchange(p, PARTNER, ["roster.read"])
    await exchanger.exchange(p, PARTNER, ["roster.read"], force=True)
    assert exchanger.exchange_count == 2


async def test_cache_is_bounded(idp: FakeIdp, clock: Clock) -> None:
    ex = TokenExchanger(idp.client(), TOKEN_URL, GATEWAY, SecretStr("s"), max_cache_entries=2, clock=clock)
    users = [await principal(idp, sub=f"user-{i}") for i in range(3)]
    for u in users:
        await ex.exchange(u, PARTNER, [])
    assert len(ex._cache) == 2
    await ex.exchange(users[0], PARTNER, [])  # evicted, so it is fetched again
    assert ex.exchange_count == 4


def _reply(status: int, body: object = None, text: str | None = None) -> Callable[..., httpx.Response]:
    def answer(_: dict[str, str]) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return answer


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_grant"}, AuthErrorCode.INVALID_TOKEN),
        (400, {"error": "invalid_token"}, AuthErrorCode.INVALID_TOKEN),
        (400, {"error": "invalid_scope"}, AuthErrorCode.INSUFFICIENT_SCOPE),
    ],
)
async def test_user_side_failures_become_auth_errors(
    idp: FakeIdp, exchanger: TokenExchanger, status: int, body: object, expected: AuthErrorCode
) -> None:
    idp.exchange_response = _reply(status, body)
    with pytest.raises(AuthError) as err:
        await exchanger.exchange(await principal(idp), PARTNER, ["roster.read"])
    assert err.value.code is expected


@pytest.mark.parametrize(
    ("status", "body", "text", "category"),
    [
        (401, {"error": "invalid_client"}, None, ErrorCategory.AUTH),
        (403, {"error": "access_denied"}, None, ErrorCategory.AUTH),
        (400, None, "<html>oops</html>", ErrorCategory.AUTH),
        (503, {"error": "server_error"}, None, ErrorCategory.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_gateway_side_failures_become_partner_errors(
    idp: FakeIdp,
    exchanger: TokenExchanger,
    status: int,
    body: object,
    text: str | None,
    category: ErrorCategory,
) -> None:
    idp.exchange_response = _reply(status, body, text)
    with pytest.raises(PartnerError) as err:
        await exchanger.exchange(await principal(idp), PARTNER, ["roster.read"])
    assert err.value.category is category


async def test_idp_unreachable(idp: FakeIdp) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    ex = TokenExchanger(
        httpx.AsyncClient(transport=httpx.MockTransport(boom)), TOKEN_URL, GATEWAY, SecretStr("s")
    )
    with pytest.raises(PartnerError) as err:
        await ex.exchange(await principal(idp), PARTNER, [])
    assert err.value.category is ErrorCategory.UPSTREAM_UNAVAILABLE


# ---- end to end: gateway -> OBO -> mock partner in OIDC mode ---------------------------


def roster_client(idp: FakeIdp, exchanger: TokenExchanger, p, scopes: list[str]) -> RosterClient:  # type: ignore[no-untyped-def]
    partner_verifier = TokenVerifier(ISSUER, PARTNER, JwksCache(JWKS_URL, idp.client()))
    app = create_app(ApiSettings(auth_mode="oidc"), verifier=partner_verifier)
    return RosterClient(
        CrewSettings(api_base_url="http://mock"),
        transport=httpx.ASGITransport(app=app),
        auth=OnBehalfOf(exchanger, p, PARTNER, scopes),
    )


async def test_planner_with_role_can_swap(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    anna = await principal(idp)
    async with roster_client(idp, exchanger, anna, ["roster.read", "swap.write"]) as client:
        swap = await client.request_swap("C0001", "C0002", "2026-12-03")
    assert swap["status"] == "pending_approval"
    assert swap["requested_by"] == "user-anna"  # the partner sees the user, not the gateway


async def test_scope_alone_is_not_enough_without_the_role(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    erik = await principal(idp, sub="user-erik")  # his app may ask for swap.write, his role says no
    async with roster_client(idp, exchanger, erik, ["roster.read", "swap.write"]) as client:
        assert len([c async for c in client.iter_crew(page_size=50)]) == 120
        with pytest.raises(PartnerError) as err:
            await client.request_swap("C0001", "C0002", "2026-12-03")
    assert err.value.category is ErrorCategory.FORBIDDEN
    assert "insufficient_role" in err.value.message


async def test_partner_rejects_the_users_own_token(idp: FakeIdp) -> None:
    """No token passthrough: a token issued for the gateway is useless at the partner."""
    partner_verifier = TokenVerifier(ISSUER, PARTNER, JwksCache(JWKS_URL, idp.client()))
    app = create_app(ApiSettings(auth_mode="oidc"), verifier=partner_verifier)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as http:
        r = await http.get("/crew", headers={"Authorization": f"Bearer {idp.token()}"})
        missing = await http.get("/crew")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_token"
    assert missing.status_code == 401


async def test_partner_scope_check_in_oidc_mode(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    anna = await principal(idp)
    async with roster_client(idp, exchanger, anna, ["roster.read"]) as client:
        with pytest.raises(PartnerError) as err:
            await client.request_swap("C0001", "C0002", "2026-12-03")
    assert "insufficient_scope" in err.value.message


async def test_partner_401_triggers_one_fresh_exchange(idp: FakeIdp, exchanger: TokenExchanger) -> None:
    anna = await principal(idp)
    async with roster_client(idp, exchanger, anna, ["roster.read"]) as client:
        await client.get_roster("C0001")
        key = next(iter(exchanger._cache))
        exchanger._cache[key] = ("revoked.token.value", exchanger._cache[key][1])
        await client.get_roster("C0002")
    assert exchanger.exchange_count == 2


def test_oidc_mode_builds_its_own_verifier() -> None:
    app = create_app(ApiSettings(auth_mode="oidc", oidc_jwks_url=JWKS_URL, oidc_issuer=ISSUER))
    assert app.title == "Mock Crew Roster API"
