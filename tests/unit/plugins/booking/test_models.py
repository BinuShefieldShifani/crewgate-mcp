"""Every Duffel quirk becomes an explicit field or note, never a silent gap."""

from crewgate.plugins.booking.models import detail_offer, summarize_offer, summarize_seat_maps

from .conftest import offer_with


def seg(o: str, d: str, bags: list | None, n: str = "101") -> dict:
    return {
        "origin": {"iata_code": o},
        "destination": {"iata_code": d},
        "marketing_carrier": {"iata_code": "ZZ"},
        "operating_carrier": {"iata_code": "BA"},
        "marketing_carrier_flight_number": n,
        "departing_at": "2026-12-20T08:00:00",
        "passengers": [{"passenger_id": "pas_1", "baggages": bags}] if bags is not None else [],
    }


def test_normal_offer_with_bags_and_codeshare() -> None:
    bags = [{"type": "checked", "quantity": 2}, {"type": "carry_on", "quantity": 1}]
    s = summarize_offer(
        offer_with(
            slices=[
                {
                    "origin": {"iata_code": "ARN"},
                    "destination": {"iata_code": "CPH"},
                    "fare_brand_name": "Light",
                    "segments": [seg("ARN", "CPH", bags)],
                }
            ],
            conditions={"refund_before_departure": {"allowed": True}},
        )
    )
    assert s.checked_bags_included == 2 and s.carry_on_bags_included == 1 and s.notes == []
    assert s.slices[0].segments[0].flight == "ZZ101" and s.slices[0].segments[0].operated_by == "BA"
    assert s.refundable is True and s.changeable is None and s.slices[0].fare_brand == "Light"


def test_lowest_allowance_across_connections_wins() -> None:
    one = [{"type": "checked", "quantity": 1}]
    none = [{"type": "carry_on", "quantity": 1}]
    s = summarize_offer(
        offer_with(slices=[{"segments": [seg("LHR", "DOH", one), seg("DOH", "DXB", none, "102")]}])
    )
    assert s.slices[0].stops == 1 and s.checked_bags_included == 0
    assert s.notes == ["No checked bag is included in this fare."]


def test_airline_reports_no_baggage_at_all() -> None:
    s = summarize_offer(offer_with(slices=[{"segments": [seg("BTS", "MRU", [])]}]))
    assert s.checked_bags_included == 0
    assert "did not report any included baggage" in s.notes[0]


def test_missing_fields_never_crash() -> None:
    s = summarize_offer({"id": "off_x", "total_amount": "1.00", "total_currency": "EUR"})
    assert s.slices == [] and s.airline is None and s.refundable is None


def test_price_check_and_extra_bags() -> None:
    svc = {
        "id": "ase_1",
        "type": "baggage",
        "total_amount": "35.00",
        "total_currency": "EUR",
        "maximum_quantity": 2,
        "metadata": {"type": "checked", "maximum_weight_kg": 23},
    }
    d = detail_offer(
        offer_with(total_amount="112.00", available_services=[svc, {"id": "ase_s", "type": "seat"}]), "100.00"
    )
    assert d.price_check == "changed" and d.quoted_total == "100.00"
    assert any("Price changed from 100.00 to 112.00 EUR" in n for n in d.notes)
    assert [b.max_weight_kg for b in d.extra_bags] == [23] and d.passengers[0].passenger_id == "pas_1"
    same = detail_offer(offer_with(total_amount="100.0"), "100.00")
    assert same.price_check == "unchanged"
    assert "no extra bags" in " ".join(same.notes)
    assert detail_offer(offer_with()).price_check == "not_checked"


def test_seat_maps() -> None:
    none = summarize_seat_maps("off_1", [])
    assert not none.seat_selection_available and "assigned at check-in" in none.message
    elements = [
        {"type": "seat", "available_services": [{"total_amount": "0.00", "total_currency": "EUR"}]},
        {"type": "seat", "available_services": [{"total_amount": "25.00", "total_currency": "EUR"}]},
        {"type": "seat", "available_services": [{"total_amount": "15.00", "total_currency": "EUR"}]},
        {"type": "seat", "available_services": []},
        {"type": "exit_row"},
    ]
    m = summarize_seat_maps(
        "off_1", [{"segment_id": "seg_1", "cabins": [{"rows": [{"sections": [{"elements": elements}]}]}]}]
    )
    seg_seats = m.segments[0]
    assert (seg_seats.seats_total, seg_seats.seats_available, seg_seats.free_seats_available) == (4, 3, 1)
    assert seg_seats.cheapest_paid_seat == "15.00" and m.seat_selection_available
    full = summarize_seat_maps(
        "off_1", [{"cabins": [{"rows": [{"sections": [{"elements": [elements[3]]}]}]}]}]
    )
    assert not full.seat_selection_available and full.message == "No seats are free."
