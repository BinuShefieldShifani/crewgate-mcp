"""The "never say confirmed" guard: a reply may claim a booking only if the order says so."""

import pytest

from crewgate.plugins.booking.confirmation import check_reply, find_claims
from crewgate.plugins.booking.state import Booking, BookingState


def booking(state: BookingState) -> Booking:
    return Booking(
        booking_id="bk_1",
        state=state,
        booking_reference="ABC123",
        new_total="420.00",
        currency="EUR",
        updated_at=0,
    )


CLAIMS = [
    "Your flight is booked!",
    "Booking confirmed.",
    "Great news, you're all set for Friday.",
    "I booked it for you, reference ABC123.",
    "Your e-tickets issued, have a nice trip.",
    "Your confirmation number is ABC123.",
    "Seats reserved for both of you.",
    "Din resa är bokad.",
    "Bokningen är bekräftad.",
]
NOT_CLAIMS = [
    "It is not confirmed yet.",
    "Your booking isn't confirmed yet, the airline is still processing it.",
    "Awaiting airline confirmation.",
    "Once it's confirmed I'll let you know.",
    "The price changed, so nothing was booked.",
    "It will be ticketed when paid.",
    "I can't tell whether it was booked, so I'm checking.",
    "Bokningen är inte bekräftad än.",
    "Ingenting är bokat.",
    "Shall I book the 08:00 flight?",
]


@pytest.mark.parametrize("reply", CLAIMS)
def test_claims_are_found(reply: str) -> None:
    assert find_claims(reply)


@pytest.mark.parametrize("reply", NOT_CLAIMS)
def test_honest_replies_are_left_alone(reply: str) -> None:
    assert find_claims(reply) == []


@pytest.mark.parametrize("state", [s for s in BookingState if s is not BookingState.CONFIRMED])
@pytest.mark.parametrize("reply", CLAIMS)
def test_claim_blocked_for_every_state_but_confirmed(state: BookingState, reply: str) -> None:
    check = check_reply(reply, [booking(state)])
    assert not check.ok and check.safe_reply == booking(state).what_to_tell_the_user


@pytest.mark.parametrize("reply", CLAIMS)
def test_claim_allowed_when_the_order_is_confirmed(reply: str) -> None:
    assert check_reply(reply, [booking(BookingState.CONFIRMED)]).ok


def test_claim_without_any_booking_is_blocked() -> None:
    check = check_reply("Your flight is booked!", [])
    assert not check.ok and "nothing has been booked" in str(check.safe_reply)


def test_one_unconfirmed_booking_is_enough_to_block() -> None:
    check = check_reply(
        "Both flights are booked.",
        [booking(BookingState.CONFIRMED), booking(BookingState.PENDING_CONFIRMATION)],
    )
    assert not check.ok and "not confirmed the booking yet" in str(check.safe_reply)


def test_reply_without_claims_passes() -> None:
    assert check_reply("Here are three flights from Stockholm.", [booking(BookingState.FAILED)]).ok
