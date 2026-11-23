"""Booking state, derived only from what Duffel's order says. Never from what anyone hopes.

Duffel orders have no single "status" field, so the state is worked out from several:

  cancelled_at set                                  -> cancelled
  payment_status.awaiting_payment                   -> awaiting_payment (a hold, not paid)
  no booking_reference                              -> pending_confirmation
  booking_reference but no electronic ticket yet    -> pending_confirmation
  booking_reference, paid, e-ticket issued          -> confirmed   <- the ONLY confirmed state

States for attempts that never became an order:
  price_changed, offer_unavailable                  -> not booked, the user must decide again
  pending_confirmation (HTTP 202)                   -> accepted, the webhook will settle it
  outcome_unknown                                   -> timed out after sending: check, don't retry
  failed                                            -> the airline or Duffel said no
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class BookingState(StrEnum):
    CONFIRMED = "confirmed"
    AWAITING_PAYMENT = "awaiting_payment"
    PENDING_CONFIRMATION = "pending_confirmation"
    OUTCOME_UNKNOWN = "outcome_unknown"
    PRICE_CHANGED = "price_changed"
    OFFER_UNAVAILABLE = "offer_unavailable"
    CANCELLED = "cancelled"
    FAILED = "failed"


WHAT_TO_SAY = {
    BookingState.CONFIRMED: "The booking is confirmed and ticketed. Booking reference {ref}.",
    BookingState.AWAITING_PAYMENT: "The seats are held but not paid, so this is not a confirmed booking yet.",
    BookingState.PENDING_CONFIRMATION: (
        "The airline has not confirmed the booking yet. Do not treat it as booked."
    ),
    BookingState.OUTCOME_UNKNOWN: (
        "We could not tell whether the booking went through. It is being checked; do not book again."
    ),
    BookingState.PRICE_CHANGED: (
        "Nothing was booked: the price changed to {new_total} {currency}. Ask before rebooking."
    ),
    BookingState.OFFER_UNAVAILABLE: "Nothing was booked: this offer is no longer available. Search again.",
    BookingState.CANCELLED: "This booking is cancelled.",
    BookingState.FAILED: "Nothing was booked: the airline refused the booking ({reason}).",
}


def derive_state(order: dict[str, Any]) -> tuple[BookingState, str | None]:
    """(state, reason) from a Duffel order object."""
    if order.get("cancelled_at"):
        return BookingState.CANCELLED, None
    payment = order.get("payment_status") or {}
    if payment.get("awaiting_payment"):
        return BookingState.AWAITING_PAYMENT, "payment_required"
    if not order.get("booking_reference"):
        return BookingState.PENDING_CONFIRMATION, "no_booking_reference"
    tickets = [
        d
        for d in order.get("documents") or []
        if d.get("type") == "electronic_ticket" and d.get("unique_identifier")
    ]
    if not tickets:
        return BookingState.PENDING_CONFIRMATION, "not_ticketed_yet"
    return BookingState.CONFIRMED, None


class Booking(BaseModel):
    booking_id: str = Field(description="crewgate's id for this booking attempt")
    state: BookingState
    order_id: str | None = None
    booking_reference: str | None = None
    total_amount: str | None = None
    currency: str | None = None
    e_tickets: int = 0
    new_total: str | None = Field(None, description="Only for price_changed: the price now")
    reason: str | None = None
    airline_change_detected: bool = False
    updated_at: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def confirmed(self) -> bool:
        """The one flag an assistant may use to say 'confirmed'."""
        return self.state is BookingState.CONFIRMED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def what_to_tell_the_user(self) -> str:
        return WHAT_TO_SAY[self.state].format(
            ref=self.booking_reference or "?",
            new_total=self.new_total or "?",
            currency=self.currency or "",
            reason=self.reason or "unknown reason",
        )


@dataclass
class _Entry:
    subject: str
    booking: Booking


class BookingStore:
    """Who owns which booking attempt, and its latest known state. In memory, one replica."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._by_id: dict[str, _Entry] = {}
        self._by_order: dict[str, str] = {}

    def new_id(self) -> str:
        return "bk_" + secrets.token_urlsafe(12)

    def put(self, subject: str, booking: Booking) -> Booking:
        self._by_id[booking.booking_id] = _Entry(subject, booking)
        if booking.order_id:
            self._by_order[booking.order_id] = booking.booking_id
        return booking

    def get(self, booking_id: str, subject: str | None = None) -> Booking | None:
        entry = self._by_id.get(booking_id)
        if entry is None or (subject is not None and entry.subject != subject):
            return None
        return entry.booking

    def all(self) -> list[tuple[str, Booking]]:
        """(subject, booking) for every attempt, e.g. to check an agent's replies against the truth."""
        return [(e.subject, e.booking) for e in self._by_id.values()]

    def find(
        self, *, booking_id: str | None = None, order_id: str | None = None
    ) -> tuple[str, Booking] | None:
        bid = booking_id if booking_id in self._by_id else self._by_order.get(order_id or "")
        if bid is None:
            return None
        entry = self._by_id[bid]
        return entry.subject, entry.booking

    def update_from_order(self, booking_id: str, order: dict[str, Any], **extra: Any) -> Booking | None:
        found = self.find(booking_id=booking_id, order_id=order.get("id"))
        if found is None:
            return None
        subject, old = found
        return self.put(
            subject,
            from_order(
                old.booking_id,
                order,
                self.clock,
                **{"airline_change_detected": old.airline_change_detected, **extra},
            ),
        )

    def mark(self, booking_id: str, state: BookingState, reason: str | None = None) -> Booking | None:
        found = self.find(booking_id=booking_id)
        if found is None:
            return None
        subject, old = found
        return self.put(
            subject,
            old.model_copy(update={"state": state, "reason": reason, "updated_at": int(self.clock())}),
        )


def from_order(
    booking_id: str, order: dict[str, Any], clock: Callable[[], float] = time.time, **extra: Any
) -> Booking:
    state, reason = derive_state(order)
    tickets = [d for d in order.get("documents") or [] if d.get("type") == "electronic_ticket"]
    return Booking(
        booking_id=booking_id,
        state=state,
        order_id=order.get("id"),
        booking_reference=order.get("booking_reference"),
        total_amount=order.get("total_amount"),
        currency=order.get("total_currency"),
        e_tickets=len(tickets),
        reason=reason,
        updated_at=int(clock()),
        **extra,
    )
