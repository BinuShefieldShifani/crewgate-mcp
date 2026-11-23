"""Contract tests against Duffel's real test-mode API.

The unit tests use a fake built from Duffel's docs. These prove our client and parsing match
the real thing, including Duffel's own scenario routes. They run in CI (duffel-contract.yml)
with the DUFFEL_TEST_TOKEN secret, nightly and on PRs that touch the booking plugin.

    DUFFEL_ACCESS_TOKEN=duffel_test_... uv run pytest -m sandbox tests/contract -v -s
"""

import datetime as dt
import os
from typing import Any

import pytest
from pydantic import SecretStr

from crewgate.core.auth import Principal
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.plugin import PluginContext
from crewgate.plugins.booking import BookingPlugin
from crewgate.plugins.booking.config import DuffelSettings
from crewgate.plugins.booking.confirmation import check_reply
from crewgate.plugins.booking.duffel import DuffelClient
from crewgate.plugins.booking.models import detail_offer, summarize_offer, summarize_seat_maps
from crewgate.plugins.booking.schemas import PassengerInput
from crewgate.plugins.booking.state import BookingState

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.skipif(
        not os.getenv("DUFFEL_ACCESS_TOKEN", "").startswith("duffel_test_"),
        reason="needs DUFFEL_ACCESS_TOKEN with a duffel_test_ token",
    ),
]
DAY = dt.date.today() + dt.timedelta(days=60)
ANNA = PassengerInput(
    title="ms",
    given_name="Anna",
    family_name="Testsson",
    born_on=dt.date(1990, 4, 2),
    gender="f",
    email="anna.testsson@example.com",
    phone_number="+46701234567",
)
ME = Principal(
    subject="contract-test",
    client_id="contract",
    username=None,
    scopes=frozenset(),
    roles={},
    expires_at=4e9,
    token=SecretStr("x"),
)


def settings() -> DuffelSettings:
    return DuffelSettings(timeout_seconds=30.0)


async def search(origin: str, destination: str) -> list[dict[str, Any]]:
    async with DuffelClient(settings()) as d:
        _, offers = await d.search(
            origin=origin,
            destination=destination,
            departure_date=DAY,
            adults=1,
            cabin_class="economy",
            max_connections=1,
        )
    return offers


def report(label: str, value: Any) -> None:
    print(f"\n[contract] {label}: {value}")  # visible with -s in the CI log


async def test_search_parses_every_real_offer() -> None:
    offers = await search("LHR", "JFK")
    assert offers, "Duffel returned no offers for LHR-JFK"
    summaries = [summarize_offer(o) for o in offers]  # must not raise on any real offer
    report("LHR-JFK offers", len(offers))
    report("airlines", sorted({s.airline for s in summaries if s.airline}))
    assert all(s.total_amount and s.currency for s in summaries)
    async with DuffelClient(settings()) as d:
        detail = detail_offer(await d.get_offer(offers[0]["id"]), summaries[0].total_amount)
        maps = summarize_seat_maps(offers[0]["id"], await d.seat_maps(offers[0]["id"]))
    report("price check", detail.price_check)
    report("seat selection", maps.seat_selection_available)
    assert detail.passengers and detail.price_check in ("unchanged", "changed")


async def test_scenario_no_flights() -> None:
    assert await search("PVD", "RAI") == []


async def test_scenario_no_baggage() -> None:
    offers = await search("BTS", "MRU")
    assert offers
    s = summarize_offer(offers[0])
    report("BTS-MRU bags", (s.checked_bags_included, s.notes))
    assert s.checked_bags_included == 0 and s.notes


async def test_scenario_timeout_is_reported_not_hung() -> None:
    with pytest.raises(PartnerError) as e:
        await search("STN", "LHR")
    report("STN-LHR", (e.value.category, e.value.code, e.value.status))
    assert e.value.category is ErrorCategory.UPSTREAM_UNAVAILABLE


async def test_scenario_price_change_books_nothing() -> None:
    offer = summarize_offer((await search("LHR", "STN"))[0])
    plugin = BookingPlugin(PluginContext(), settings())
    result = await plugin.book_flight(
        ME,
        offer_id=offer.offer_id,
        expected_total=offer.total_amount,
        currency=offer.currency,
        market="SE",
        passengers=[ANNA],
    )
    report("LHR-STN booking", (result.state, result.new_total, result.reason))
    assert result.state in (BookingState.PRICE_CHANGED, BookingState.OFFER_UNAVAILABLE)
    assert not result.confirmed and not check_reply("Your flight is booked!", [result]).ok


async def test_full_booking_on_duffel_airways() -> None:
    offers = await search("LHR", "JFK")
    duffel_airways = [o for o in offers if (o.get("owner") or {}).get("name") == "Duffel Airways"] or offers
    offer = summarize_offer(sorted(duffel_airways, key=lambda o: float(o["total_amount"]))[0])
    plugin = BookingPlugin(PluginContext(), settings())
    result = await plugin.book_flight(
        ME,
        offer_id=offer.offer_id,
        expected_total=offer.total_amount,
        currency=offer.currency,
        market="SE",
        passengers=[ANNA],
    )
    report("booking", (result.state, result.booking_reference, result.e_tickets, result.reason))
    assert result.state in (
        BookingState.CONFIRMED,
        BookingState.PENDING_CONFIRMATION,
        BookingState.PRICE_CHANGED,
    )
    if result.state is BookingState.CONFIRMED:
        assert result.booking_reference and result.e_tickets >= 1
        again = await plugin.get_booking(ME, booking_id=result.booking_id)  # re-read from Duffel
        assert again.confirmed
    claim = check_reply("Your flight is booked!", [result])
    assert claim.ok is result.confirmed  # the guard agrees with the order, both ways
