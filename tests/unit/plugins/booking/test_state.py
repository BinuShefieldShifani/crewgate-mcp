"""Booking state comes only from the order. Only one state is 'confirmed'."""

import pytest

from crewgate.plugins.booking.state import Booking, BookingState, BookingStore, derive_state, from_order

TICKET = [{"type": "electronic_ticket", "unique_identifier": "1251234567890"}]
PAID = {"awaiting_payment": False}


@pytest.mark.parametrize(
    ("order", "state", "reason"),
    [
        (
            {"booking_reference": "ABC123", "payment_status": PAID, "documents": TICKET},
            BookingState.CONFIRMED,
            None,
        ),
        (
            {"booking_reference": "ABC123", "payment_status": PAID, "documents": []},
            BookingState.PENDING_CONFIRMATION,
            "not_ticketed_yet",
        ),
        (
            {"booking_reference": "ABC123", "documents": [{"type": "electronic_ticket"}]},
            BookingState.PENDING_CONFIRMATION,
            "not_ticketed_yet",
        ),
        (
            {"payment_status": PAID, "documents": TICKET},
            BookingState.PENDING_CONFIRMATION,
            "no_booking_reference",
        ),
        (
            {
                "booking_reference": "ABC123",
                "payment_status": {"awaiting_payment": True},
                "documents": TICKET,
            },
            BookingState.AWAITING_PAYMENT,
            "payment_required",
        ),
        (
            {"booking_reference": "ABC123", "cancelled_at": "2026-11-21T10:00:00Z", "documents": TICKET},
            BookingState.CANCELLED,
            None,
        ),
        ({}, BookingState.PENDING_CONFIRMATION, "no_booking_reference"),
    ],
)
def test_state_is_derived_from_the_order(order: dict, state: BookingState, reason: str | None) -> None:
    assert derive_state(order) == (state, reason)


def test_only_confirmed_is_confirmed() -> None:
    for state in BookingState:
        b = Booking(
            booking_id="bk_1", state=state, updated_at=0, booking_reference="ABC123", new_total="1.00"
        )
        assert b.confirmed is (state is BookingState.CONFIRMED)
        assert b.model_dump()["confirmed"] is b.confirmed  # it is in the tool output, not just the class
        said = b.what_to_tell_the_user.lower()
        if state is BookingState.CONFIRMED:
            assert "abc123" in said
        else:
            assert "confirmed and ticketed" not in said


def test_store_keeps_bookings_per_person() -> None:
    store = BookingStore(clock=lambda: 100.0)
    order = {"id": "ord_1", "booking_reference": "ABC123", "payment_status": PAID, "documents": TICKET}
    store.put("user-anna", from_order("bk_1", order))
    assert store.get("bk_1", subject="user-anna") is not None
    assert store.get("bk_1", subject="user-erik") is None  # someone else's booking looks missing
    assert store.find(order_id="ord_1") is not None and store.find(booking_id="nope") is None
    cancelled = store.update_from_order("bk_1", {**order, "cancelled_at": "2026-11-22T00:00:00Z"})
    assert cancelled is not None and cancelled.state is BookingState.CANCELLED
    assert store.update_from_order("bk_missing", {"id": "ord_x"}) is None
    assert (
        store.mark("bk_1", BookingState.FAILED, "x") is not None
        and store.mark("bk_missing", BookingState.FAILED) is None
    )
