"""Key caching, rotation and flood protection."""

import httpx
import pytest

from crewgate.core.auth import AuthError, AuthErrorCode, JwksCache
from tests.helpers.idp import JWKS_URL, FakeIdp, rsa_key


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


def cache(idp: FakeIdp, clock: Clock, **kw: float) -> JwksCache:
    return JwksCache(JWKS_URL, idp.client(), clock=clock, **kw)


async def test_keys_are_cached(clock: Clock) -> None:
    idp = FakeIdp()
    c = cache(idp, clock)
    await c.get_key("rsa-1")
    await c.get_key("rsa-1")
    assert idp.jwks_calls == 1


async def test_refreshes_after_ttl(clock: Clock) -> None:
    idp = FakeIdp()
    c = cache(idp, clock, ttl_seconds=300)
    await c.get_key("rsa-1")
    clock.t += 301
    await c.get_key("rsa-1")
    assert idp.jwks_calls == 2


async def test_key_rotation_is_picked_up(clock: Clock) -> None:
    idp = FakeIdp()
    c = cache(idp, clock, min_refresh_interval_seconds=10)
    await c.get_key("rsa-1")
    idp.keys.append(rsa_key(kid="rsa-2"))
    clock.t += 11
    assert (await c.get_key("rsa-2")).jwk.key_id == "rsa-2"
    assert idp.jwks_calls == 2


async def test_unknown_kid_flood_cannot_hammer_the_idp(clock: Clock) -> None:
    idp = FakeIdp()
    c = cache(idp, clock, min_refresh_interval_seconds=10)
    await c.get_key("rsa-1")
    for i in range(50):
        with pytest.raises(AuthError):
            await c.get_key(f"made-up-{i}")
    assert idp.jwks_calls == 1


async def test_encryption_and_broken_keys_are_skipped(clock: Clock) -> None:
    good = rsa_key(kid="rsa-1").jwk
    enc = {**rsa_key(kid="enc-1").jwk, "use": "enc"}  # Keycloak publishes an RSA-OAEP key like this
    broken = {"kid": "broken", "kty": "RSA", "n": "%%%", "e": "AQAB"}
    no_kid = {k: v for k, v in rsa_key().jwk.items() if k != "kid"}
    body = {"keys": [good, enc, broken, no_kid]}
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    c = JwksCache(JWKS_URL, http, clock=clock)
    assert (await c.get_key("rsa-1")).alg == "RS256"
    for kid in ("enc-1", "broken"):
        with pytest.raises(AuthError):
            await c.get_key(kid)


async def test_idp_down_and_no_cached_keys_is_503(clock: Clock) -> None:
    idp = FakeIdp(jwks_status=503)
    with pytest.raises(AuthError) as err:
        await cache(idp, clock).get_key("rsa-1")
    assert err.value.code is AuthErrorCode.TEMPORARILY_UNAVAILABLE
    assert err.value.status == 503


async def test_idp_down_keeps_last_good_keys(clock: Clock) -> None:
    idp = FakeIdp()
    c = cache(idp, clock, ttl_seconds=300)
    await c.get_key("rsa-1")
    idp.jwks_status = 500
    clock.t += 301
    assert (await c.get_key("rsa-1")).jwk.key_id == "rsa-1"
