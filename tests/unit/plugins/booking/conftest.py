import httpx
import pytest
from pydantic import SecretStr

from crewgate.plugins.booking.config import DuffelSettings
from crewgate.plugins.booking.duffel import DuffelClient
from tests.helpers.fake_duffel import FakeDuffel

TOKEN = SecretStr("duffel_test_" + "x" * 20)  # placeholder, never a real token


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def fake() -> FakeDuffel:
    return FakeDuffel()


@pytest.fixture
def duffel(fake: FakeDuffel) -> DuffelClient:
    return DuffelClient(DuffelSettings(access_token=TOKEN), transport=fake.transport(), sleep=_no_sleep)


def offer_with(**over: object) -> dict:
    """A minimal Duffel offer in the documented shape."""
    base: dict = {
        "id": "off_1",
        "total_amount": "100.00",
        "total_currency": "EUR",
        "owner": {"iata_code": "ZZ", "name": "Duffel Airways"},
        "passengers": [{"id": "pas_1", "type": "adult"}],
        "slices": [],
    }
    base.update(over)
    return base


__all__ = ["TOKEN", "_no_sleep", "httpx"]
