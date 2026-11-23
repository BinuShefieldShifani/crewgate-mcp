"""The last step of every agent turn: the draft reply is checked against the facts.

Facts are what the gateway's tools returned during the conversation: booking states (from
booking_get_booking, or an executed approval) and approval requests still waiting. The model's
words never count as a fact.

A draft that claims a flight is booked or confirmed is replaced unless every booking it could
be about is CONFIRMED. A draft about crew ("Erik is confirmed on standby") is left alone: the
check applies once the conversation touched booking tools or the draft talks about flights.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from crewgate.plugins.booking.confirmation import check_reply, find_claims
from crewgate.plugins.booking.state import Booking

_FLIGHT_WORDS = re.compile(
    r"\b(flights?|fly|flying|booking|bookings|ticket|tickets|ticketed|seats?|fare|itinerary|"
    r"flyg|resa|biljett|bokning)\b",
    re.IGNORECASE,
)
AWAITING_APPROVAL = (
    "Nothing is booked yet. The booking is waiting for approval: open the approval link to approve "
    "or reject it, and I will check the result afterwards."
)


@dataclass(frozen=True)
class GuardDecision:
    ok: bool
    reply: str  # what the user sees
    draft: str  # what the model wrote
    claims: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class Facts:
    bookings: dict[str, dict[str, Any]]  # booking_id -> Booking as JSON
    approvals: dict[str, dict[str, Any]]  # approval_id -> {"tool": ..., "status": ...}
    booking_tools_used: bool = False

    def booking_models(self) -> list[Booking]:
        out = []
        for raw in self.bookings.values():
            try:
                out.append(
                    Booking.model_validate({k: v for k, v in raw.items() if k in Booking.model_fields})
                )
            except ValidationError:
                continue
        return out


def facts_from_tool(name: str, is_error: bool, data: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """(bookings, approvals) updates from one tool result."""
    bookings: dict[str, Any] = {}
    approvals: dict[str, Any] = {}
    if is_error:
        return bookings, approvals
    if data.get("status") == "awaiting_approval" and data.get("approval_id"):
        approvals[str(data["approval_id"])] = {"tool": name, "status": "pending"}
    if name == "booking_get_booking" and data.get("booking_id"):
        bookings[str(data["booking_id"])] = dict(data)
    if name == "gateway_approval_status" and data.get("approval_id"):
        approvals[str(data["approval_id"])] = {"tool": data.get("tool"), "status": data.get("status")}
        result = data.get("result") or {}
        if isinstance(result, dict) and result.get("booking_id") and result.get("state"):
            bookings[str(result["booking_id"])] = dict(result)
    return bookings, approvals


def guard_reply(draft: str, facts: Facts) -> GuardDecision:
    if not facts.booking_tools_used and not _FLIGHT_WORDS.search(draft):
        return GuardDecision(True, draft, draft)
    bookings = facts.booking_models()
    check = check_reply(draft, bookings)
    if check.ok:
        return GuardDecision(True, draft, draft, check.claims)
    waiting = [
        a
        for a in facts.approvals.values()
        if a.get("tool") == "booking.book_flight" or a.get("tool") == "booking_book_flight"
    ]
    waiting = [a for a in waiting if a.get("status") in ("pending", "approved")]
    if not bookings and waiting:
        return GuardDecision(False, AWAITING_APPROVAL, draft, check.claims, "awaiting_approval")
    return GuardDecision(False, check.safe_reply or AWAITING_APPROVAL, draft, check.claims, "not_confirmed")


__all__ = ["AWAITING_APPROVAL", "Facts", "GuardDecision", "facts_from_tool", "find_claims", "guard_reply"]
