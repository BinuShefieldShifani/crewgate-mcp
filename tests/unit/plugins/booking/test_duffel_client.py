"""The Duffel client against the fake: headers, timeouts, error codes, safe retries."""

import datetime as dt

import pytest
from pydantic import SecretStr, ValidationError

from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.plugins.booking.config import DuffelSettings
from crewgate.plugins.booking.duffel import DuffelClient, NotConfigured
from tests.helpers.fake_duffel import FakeDuffel

DAY = dt.date.today() + dt.timedelta(days=30)


def q(origin: str, destination: str, adults: int = 1) -> dict:
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": DAY,
        "adults": adults,
        "cabin_class": "economy",
        "max_connections": 1,
    }


async def test_required_headers_and_supplier_timeout(duffel: DuffelClient, fake: FakeDuffel) -> None:
    offer_request, offers = await duffel.search(**q("ARN", "CPH"))
    assert offer_request["id"].startswith("orq_") and len(offers) == 3
    first = fake.calls[0]
    assert first.headers["duffel-version"] == "v2"
    assert first.headers["authorization"].startswith("Bearer duffel_test_")
    assert first.headers["accept"] == "application/json" and "gzip" in first.headers["accept-encoding"]
    assert first.url.params["supplier_timeout"] == "8000" and first.url.params["return_offers"] == "false"


async def test_search_timeout_is_retried_then_reported(duffel: DuffelClient, fake: FakeDuffel) -> None:
    with pytest.raises(PartnerError) as e:
        await duffel.search(**q("STN", "LHR"))
    assert e.value.category is ErrorCategory.UPSTREAM_UNAVAILABLE
    assert len(fake.posts("/air/offer_requests")) == 3  # a search is safe to retry


async def test_duffel_codes_become_categories(duffel: DuffelClient, fake: FakeDuffel) -> None:
    _, offers = await duffel.search(**q("LGW", "LHR"))
    with pytest.raises(PartnerError) as e:
        await duffel.get_offer(offers[0]["id"])
    assert e.value.category is ErrorCategory.CONFLICT and e.value.code == "offer_no_longer_available"


async def test_order_timeout_is_outcome_unknown_and_sent_once(duffel: DuffelClient, fake: FakeDuffel) -> None:
    _, offers = await duffel.search(**q("ARN", "OSL"))
    o = offers[0]
    with pytest.raises(PartnerError) as e:
        await duffel.create_order(
            offer_id=o["id"],
            passengers=[{"id": o["passengers"][0]["id"]}],
            amount=o["total_amount"],
            currency=o["total_currency"],
            metadata={},
        )
    assert e.value.category is ErrorCategory.OUTCOME_UNKNOWN
    assert (
        len(fake.posts("/air/orders")) == 1 and len(fake.orders) == 1
    )  # it did book, we must not book again


async def test_rate_limit_waits_for_ratelimit_reset(duffel: DuffelClient, fake: FakeDuffel) -> None:
    fake.rate_limit_next = 1
    _, offers = await duffel.search(**q("ARN", "CPH"))
    assert len(offers) == 3
    assert [e.status for e in duffel.partner.retry_log] == [429]


async def test_accepted_order_returns_none(duffel: DuffelClient, fake: FakeDuffel) -> None:
    _, offers = await duffel.search(**q("SEN", "STN"))
    o = offers[0]
    got = await duffel.create_order(
        offer_id=o["id"],
        passengers=[{"id": o["passengers"][0]["id"]}],
        amount=o["total_amount"],
        currency=o["total_currency"],
        metadata={"crewgate_booking_id": "bk_1"},
    )
    assert got is None and len(fake.pending) == 1


async def test_without_a_token_nothing_is_sent(fake: FakeDuffel) -> None:
    async with DuffelClient(DuffelSettings(access_token=None), transport=fake.transport()) as d:
        assert not d.configured
        with pytest.raises(NotConfigured):
            await d.get_offer("off_1")
    assert fake.calls == []


def test_empty_token_means_not_configured() -> None:
    assert DuffelSettings(access_token=SecretStr("  ")).access_token is None


def test_live_tokens_are_refused() -> None:
    with pytest.raises(ValidationError, match="test-mode"):
        DuffelSettings(access_token=SecretStr("duffel_live_" + "x" * 20))
    with pytest.raises(ValidationError):
        DuffelSettings(supplier_timeout_ms=100)
