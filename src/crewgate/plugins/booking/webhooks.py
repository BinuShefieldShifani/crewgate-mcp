"""Duffel webhooks: verified, de-duplicated, and used to settle bookings.

  POST /webhooks/duffel
  1. verify X-Duffel-Signature (HMAC-SHA256 over "<t>." + raw body), inside a 5 minute window
     (Duffel's sample code checks neither the window nor duplicates; we do both)
  2. drop duplicates by idempotency_key (Duffel retries failed deliveries)
  3. order.created / order.updated      -> re-derive the booking state from the order
     order.creation_failed               -> the pending booking failed
     order.airline_initiated_change_detected -> flag the booking so the agent can tell the user
  4. answer 200 for any verified event, even types we ignore, so Duffel stops retrying

Bookings are matched by the crewgate_booking_id we put in the order's metadata, which also
works for HTTP 202 orders that had no order id when we created them.
"""

import json
import logging
from typing import Any

from crewgate.core.audit import Decision
from crewgate.core.plugin import PluginContext
from crewgate.core.signing import SignatureError, WebhookVerifier

from .config import DuffelSettings
from .state import BookingState, BookingStore

log = logging.getLogger("crewgate.booking.webhooks")


def _booking_id(obj: dict[str, Any]) -> str | None:
    meta = obj.get("metadata") or {}
    value = meta.get("crewgate_booking_id")
    return str(value) if value else None


class DuffelWebhooks:
    def __init__(self, settings: DuffelSettings, store: BookingStore, ctx: PluginContext) -> None:
        self.store, self.ctx = store, ctx
        self.verifier = (
            WebhookVerifier(settings.webhook_secrets, settings.webhook_tolerance_seconds, store.clock)
            if settings.webhook_secrets
            else None
        )

    def _count(self, event_type: str, outcome: str) -> None:
        if self.ctx.metrics is not None:
            self.ctx.metrics.webhooks.labels("duffel", event_type, outcome).inc()

    async def handle(self, signature: str | None, raw: bytes) -> tuple[int, dict[str, Any]]:
        if self.verifier is None:
            return 503, {"error": "webhooks_not_configured"}
        try:
            self.verifier.verify(signature, raw)
        except SignatureError as exc:
            if self.ctx.metrics is not None:
                self.ctx.metrics.signature_failures.labels("duffel", exc.reason.value).inc()
            return 401, {"error": exc.reason.value}
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except ValueError:
            return 400, {"error": "invalid_json"}

        event_type = str(event.get("type") or "unknown")
        key = str(event.get("idempotency_key") or event.get("id") or "")
        if not key or not self.verifier.first_delivery(key):
            self._count(event_type, "duplicate")
            return 200, {"status": "duplicate"}

        obj = ((event.get("data") or {}).get("object")) or {}
        outcome = self._apply(event_type, obj if isinstance(obj, dict) else {})
        self._count(event_type, outcome)
        if self.ctx.audit is not None:
            self.ctx.audit.record(
                subject="partner:duffel",
                client_id="duffel-webhook",
                tool="booking.webhook",
                decision=Decision.ALLOWED,
                reason=event_type,
                outcome=outcome,
                args={"event_id": event.get("id"), "type": event_type, "live_mode": event.get("live_mode")},
            )
        return 200, {"status": outcome}

    def _apply(self, event_type: str, obj: dict[str, Any]) -> str:
        if event_type == "ping.triggered":
            return "pong"
        booking_id = _booking_id(obj)
        found = self.store.find(booking_id=booking_id, order_id=obj.get("id"))
        if found is None:
            return "ignored_unknown_booking"
        _, booking = found
        if event_type in ("order.created", "order.updated"):
            self.store.update_from_order(booking.booking_id, obj)
            return "booking_updated"
        if event_type == "order.creation_failed":
            self.store.mark(booking.booking_id, BookingState.FAILED, "order_creation_failed")
            return "booking_failed"
        if event_type == "order.airline_initiated_change_detected":
            self.store.put(
                found[0],
                booking.model_copy(
                    update={"airline_change_detected": True, "updated_at": int(self.store.clock())}
                ),
            )
            return "airline_change_flagged"
        return "ignored_type"
