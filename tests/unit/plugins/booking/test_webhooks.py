"""Duffel webhooks: signature, replay window, duplicates, and settling a 202 booking."""

import json
import time

import pytest
from pydantic import SecretStr

from crewgate.core.audit import AuditLog, MemorySink
from crewgate.core.metrics import GatewayMetrics
from crewgate.core.plugin import PluginContext
from crewgate.core.signing import sign_webhook
from crewgate.plugins.booking import BookingPlugin
from crewgate.plugins.booking.config import DuffelSettings
from crewgate.plugins.booking.state import Booking, BookingState
from tests.helpers.fake_duffel import FakeDuffel

SECRET = SecretStr("dev-only-duffel-webhook-secret")
ORDER = {
    "id": "ord_1",
    "booking_reference": "ABC123",
    "payment_status": {"awaiting_payment": False},
    "documents": [{"type": "electronic_ticket", "unique_identifier": "1251234567890"}],
    "metadata": {"crewgate_booking_id": "bk_pending"},
}


@pytest.fixture
def plugin() -> BookingPlugin:
    ctx = PluginContext(
        metrics=GatewayMetrics(), audit=AuditLog(SecretStr("dev-only-audit-key"), MemorySink())
    )
    p = BookingPlugin(ctx, DuffelSettings(webhook_secrets=[SECRET]))
    p.store.put(
        "user-anna", Booking(booking_id="bk_pending", state=BookingState.PENDING_CONFIRMATION, updated_at=0)
    )
    return p


async def deliver(plugin: BookingPlugin, raw: bytes, *, secret: SecretStr = SECRET, at: int | None = None):  # type: ignore[no-untyped-def]
    return await plugin.webhooks.handle(sign_webhook(secret, raw, at or int(time.time())), raw)


async def test_order_created_settles_a_202_booking(plugin: BookingPlugin) -> None:
    status, body = await deliver(plugin, FakeDuffel.event("order.created", ORDER))
    assert (status, body) == (200, {"status": "booking_updated"})
    b = plugin.store.get("bk_pending")
    assert b is not None and b.state is BookingState.CONFIRMED and b.order_id == "ord_1"
    audit = json.loads(plugin.ctx.audit.sink.lines[0])  # type: ignore[union-attr]
    assert audit["tool"] == "booking.webhook" and audit["reason"] == "order.created"


async def test_creation_failed_and_airline_change(plugin: BookingPlugin) -> None:
    await deliver(
        plugin, FakeDuffel.event("order.creation_failed", {"metadata": {"crewgate_booking_id": "bk_pending"}})
    )
    assert plugin.store.get("bk_pending").state is BookingState.FAILED  # type: ignore[union-attr]
    _, body = await deliver(plugin, FakeDuffel.event("order.airline_initiated_change_detected", ORDER))
    assert body["status"] == "airline_change_flagged"
    assert plugin.store.get("bk_pending").airline_change_detected  # type: ignore[union-attr]


async def test_duplicates_are_dropped(plugin: BookingPlugin) -> None:
    raw = FakeDuffel.event("order.created", ORDER, key="same-key")
    assert (await deliver(plugin, raw))[1]["status"] == "booking_updated"
    again = FakeDuffel.event(
        "order.updated", {**ORDER, "cancelled_at": "2026-11-22T00:00:00Z"}, key="same-key"
    )
    assert (await deliver(plugin, again))[1] == {"status": "duplicate"}
    assert plugin.store.get("bk_pending").state is BookingState.CONFIRMED  # type: ignore[union-attr]


async def test_unknown_bookings_and_types_still_get_200(plugin: BookingPlugin) -> None:
    assert (await deliver(plugin, FakeDuffel.event("order.created", {"id": "ord_x"})))[1][
        "status"
    ] == "ignored_unknown_booking"
    assert (await deliver(plugin, FakeDuffel.event("ping.triggered", {})))[1]["status"] == "pong"
    assert (await deliver(plugin, FakeDuffel.event("stays.booking.created", ORDER)))[1][
        "status"
    ] == "ignored_type"


@pytest.mark.parametrize(
    ("how", "status", "error"),
    [
        ("unsigned", 401, "missing_signature"),
        ("wrong_secret", 401, "signature_mismatch"),
        ("stale", 401, "stale_timestamp"),
        ("tampered", 401, "signature_mismatch"),
    ],
)
async def test_unverified_webhooks_change_nothing(
    plugin: BookingPlugin, how: str, status: int, error: str
) -> None:
    raw = FakeDuffel.event("order.created", ORDER)
    if how == "unsigned":
        result = await plugin.webhooks.handle(None, raw)
    elif how == "wrong_secret":
        result = await deliver(plugin, raw, secret=SecretStr("dev-only-attacker"))
    elif how == "stale":
        result = await deliver(plugin, raw, at=int(time.time()) - 3600)
    else:
        header = sign_webhook(SECRET, raw, int(time.time()))
        result = await plugin.webhooks.handle(header, raw.replace(b"ABC123", b"EVIL99"))
    assert result == (status, {"error": error})
    assert plugin.store.get("bk_pending").state is BookingState.PENDING_CONFIRMATION  # type: ignore[union-attr]
    assert plugin.ctx.metrics.value("crewgate_signature_failures_total", source="duffel", reason=error) == 1  # type: ignore[union-attr]


async def test_bad_json_and_missing_config() -> None:
    p = BookingPlugin(PluginContext(), DuffelSettings(webhook_secrets=[SECRET]))
    assert (await p.webhooks.handle(sign_webhook(SECRET, b"not json", int(time.time())), b"not json"))[
        0
    ] == 400
    assert (await p.webhooks.handle(sign_webhook(SECRET, b"[1]", int(time.time())), b"[1]"))[0] == 400
    unconfigured = BookingPlugin(PluginContext(), DuffelSettings())
    assert (await unconfigured.webhooks.handle("t=1,v1=00", b"{}")) == (
        503,
        {"error": "webhooks_not_configured"},
    )
