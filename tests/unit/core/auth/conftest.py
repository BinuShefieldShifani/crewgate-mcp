import pytest

from crewgate.core.auth import JwksCache, TokenVerifier
from tests.helpers.idp import GATEWAY, ISSUER, JWKS_URL, FakeIdp


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def verifier(idp: FakeIdp) -> TokenVerifier:
    return TokenVerifier(ISSUER, GATEWAY, JwksCache(JWKS_URL, idp.client()))
