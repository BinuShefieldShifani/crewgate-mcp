"""The agent's last step: draft replies checked against facts from tool results."""

from typing import Any

import pytest

from crewgate.agent.reply_guard import AWAITING_APPROVAL, Facts, facts_from_tool, guard_reply
from crewgate.plugins.booking.state import Booking, BookingState


def booking(state: BookingState, **kw: Any) -> dict[str, Any]:
    return Booking(booking_id="bk_1", state=state, updated_at=0, **kw).model_dump(mode="json")


def test_records_pending_approvals_from_any_write() -> None:
    _, approvals = facts_from_tool(
        "booking_book_flight", False, {"status": "awaiting_approval", "approval_id": "apr_1"}
    )
    assert approvals == {"apr_1": {"tool": "booking_book_flight", "status": "pending"}}


def test_records_booking_state_and_executed_approvals() -> None:
    b, _ = facts_from_tool("booking_get_booking", False, booking(BookingState.CONFIRMED))
    assert b["bk_1"]["state"] == "confirmed"
    data = {
        "approval_id": "apr_1",
        "tool": "booking.book_flight",
        "status": "executed",
        "result": booking(BookingState.FAILED),
    }
    b, a = facts_from_tool("gateway_approval_status", False, data)
    assert b["bk_1"]["state"] == "failed" and a["apr_1"]["status"] == "executed"


def test_errors_are_not_facts() -> None:
    assert facts_from_tool("booking_get_booking", True, {"booking_id": "bk_1", "state": "confirmed"}) == (
        {},
        {},
    )


def test_crew_talk_is_left_alone() -> None:
    d = guard_reply("Erik is confirmed on standby on 5 December.", Facts({}, {}, False))
    assert d.ok and d.reply == d.draft


def test_made_up_booking_is_blocked_even_without_tools() -> None:
    d = guard_reply("Your flight is booked!", Facts({}, {}, False))
    assert not d.ok and "nothing has been booked" in d.reply and d.claims == ("booked",)


def test_awaiting_approval_gets_its_own_message() -> None:
    facts = Facts({}, {"apr_1": {"tool": "booking.book_flight", "status": "pending"}}, True)
    d = guard_reply("Done, your flight is booked.", facts)
    assert not d.ok and d.reply == AWAITING_APPROVAL and d.reason == "awaiting_approval"


@pytest.mark.parametrize("state", [s for s in BookingState if s is not BookingState.CONFIRMED])
def test_unconfirmed_states_say_what_the_order_says(state: BookingState) -> None:
    facts = Facts({"bk_1": booking(state, new_total="300.00", currency="EUR")}, {}, True)
    d = guard_reply("All set, you're flying on Friday!", facts)
    assert not d.ok and d.reason == "not_confirmed"
    assert d.reply == Booking.model_validate(facts.bookings["bk_1"]).what_to_tell_the_user


def test_true_claims_pass() -> None:
    facts = Facts({"bk_1": booking(BookingState.CONFIRMED, booking_reference="ABC123")}, {}, True)
    assert guard_reply("Your flight is confirmed, reference ABC123.", facts).ok


def test_unreadable_booking_data_is_ignored() -> None:
    facts = Facts({"bk_x": {"booking_id": "bk_x", "state": "nonsense"}}, {}, True)
    assert facts.booking_models() == []
    assert not guard_reply("It is booked.", facts).ok
