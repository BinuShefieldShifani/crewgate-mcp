"""Booking through the whole gateway, raw JSON-RPC, fake Duffel behind it.

Covers the happy path, every quirk (price change, no seat map, no bags, timeouts, 202, unknown
outcome) and the rule that matters most: the assistant can only call a booking confirmed when
the order says so.
"""

import datetime as dt
import json
import time
from typing import Any

import jsonschema
import pytest
from pydantic import SecretStr

from crewgate.core.signing import sign_webhook
from crewgate.plugins.booking.confirmation import check_reply
from crewgate.plugins.booking.state import Booking

from .conftest import DUFFEL_WEBHOOK_SECRET, Env

SCOPES = "openid booking.read booking.write"
DAY = (dt.date.today() + dt.timedelta(days=30)).isoformat()
PERSON = {
    "title": "ms",
    "given_name": "Anna",
    "family_name": "Berg",
    "born_on": "1990-04-02",
    "gender": "f",
    "email": "anna@example.com",
    "phone_number": "+46701234567",
}


def token(env: Env, **kw: Any) -> str:
    kw.setdefault("scope", SCOPES)
    return env.agent_token(**kw)


async def tool(env: Env, name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
    tok = kw.pop("tok", None) or token(env, **kw)
    result: dict[str, Any] = (await env.call(name, args, token=tok))["result"]
    return result


def error_of(result: dict[str, Any]) -> dict[str, Any]:
    assert result["isError"] is True, result
    body: dict[str, Any] = json.loads(result["content"][0]["text"])
    return body


async def search(env: Env, origin: str, destination: str, **extra: Any) -> dict[str, Any]:
    args = {"origin": origin, "destination": destination, "departure_date": DAY, "market": "SE", **extra}
    result = await tool(env, "booking_search_flights", args)
    assert result["isError"] is False, result
    data: dict[str, Any] = result["structuredContent"]
    return data


async def book(env: Env, origin: str, destination: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Search, propose, have the person approve. Returns (offer, decision body)."""
    offer = (await search(env, origin, destination))["offers"][0]
    args = {
        "offer_id": offer["offer_id"],
        "expected_total": offer["total_amount"],
        "currency": offer["currency"],
        "market": "SE",
        "passengers": [PERSON],
    }
    pending = (await tool(env, "booking_book_flight", args))["structuredContent"]
    assert pending["status"] == "awaiting_approval"
    assert env.duffel.posts("/air/orders") == []  # nothing is booked before a person approves
    approver = env.approval_token(scope="openid booking.write")
    r = await env.http.post(
        f"/approvals/{pending['approval_id']}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {approver}"},
    )
    assert r.status_code == 200, r.text
    return offer, r.json()


async def get_booking(env: Env, booking_id: str) -> dict[str, Any]:
    result = await tool(env, "booking_get_booking", {"booking_id": booking_id})
    assert result["isError"] is False, result
    data: dict[str, Any] = result["structuredContent"]
    return data


# ---- reads ------------------------------------------------------------------------------------


async def test_search_is_structured_and_cheapest_first(env: Env) -> None:
    tools = {t["name"]: t for t in (await env.rpc("tools/list", token=token(env))).json()["result"]["tools"]}
    found = await search(env, "ARN", "CPH")
    jsonschema.validate(found, tools["booking_search_flights"]["outputSchema"])
    prices = [float(o["total_amount"]) for o in found["offers"]]
    assert prices == sorted(prices) and found["total_found"] == 3
    first = found["offers"][0]
    assert (
        first["airline"] == "Duffel Airways" and first["checked_bags_included"] == 1 and first["notes"] == []
    )
    assert "market" in tools["booking_search_flights"]["inputSchema"]["required"]
    assert tools["booking_book_flight"]["annotations"]["destructiveHint"] is True
    assert "does NOT book anything" in tools["booking_book_flight"]["description"]


async def test_markets_entitlements_and_scopes(env: Env) -> None:
    args = {"origin": "ARN", "destination": "CPH", "departure_date": DAY, "market": "US"}
    assert error_of(await tool(env, "booking_search_flights", args))["error"] == "market_not_entitled"
    se_only = token(env, azp="booking-assistant-se")
    assert (
        error_of(await tool(env, "booking_search_flights", {**args, "market": "NO"}, tok=se_only))["error"]
        == "market_not_entitled"
    )
    assert (await tool(env, "booking_search_flights", {**args, "market": "SE"}, tok=se_only))[
        "isError"
    ] is False
    crew_only = token(env, scope="openid roster.read")
    assert (
        error_of(await tool(env, "booking_search_flights", {**args, "market": "SE"}, tok=crew_only))["error"]
        == "insufficient_scope"
    )
    dashboard = token(env, azp="roster-dashboard")
    assert (
        error_of(await tool(env, "booking_search_flights", {**args, "market": "SE"}, tok=dashboard))["error"]
        == "tool_not_entitled"
    )


async def test_bad_search_arguments(env: Env) -> None:
    past = {"origin": "ARN", "destination": "CPH", "departure_date": "2020-01-01", "market": "SE"}
    assert error_of(await tool(env, "booking_search_flights", past))["error"] == "invalid_arguments"
    lower = {**past, "departure_date": DAY, "origin": "arn"}
    assert error_of(await tool(env, "booking_search_flights", lower))["details"][0]["field"] == "origin"


async def test_quirks_reach_the_caller_as_plain_answers(env: Env) -> None:
    # slow airline: the caller gets a clear, safe error, not a hang or a stack trace
    slow = {"origin": "STN", "destination": "LHR", "departure_date": DAY, "market": "SE"}
    assert error_of(await tool(env, "booking_search_flights", slow)) == {
        "error": "partner_upstream_unavailable",
        "message": "the partner is unavailable, try again later",
    }
    none = await search(env, "PVD", "RAI")
    assert none["offers"] == [] and "No flights found" in none["message"]
    no_bags = (await search(env, "BTS", "MRU"))["offers"][0]
    assert (
        no_bags["checked_bags_included"] == 0 and "did not report any included baggage" in no_bags["notes"][0]
    )
    connecting = (await search(env, "LHR", "DXB"))["offers"][0]
    assert connecting["slices"][0]["stops"] == 1

    abv = (await search(env, "BTS", "ABV"))["offers"][0]["offer_id"]
    seats = (await tool(env, "booking_get_seat_map", {"offer_id": abv}))["structuredContent"]
    assert seats["seat_selection_available"] is False and "check-in" in seats["message"]
    offer = (await tool(env, "booking_get_offer", {"offer_id": abv}))["structuredContent"]
    assert offer["extra_bags"] == [] and any("no extra bags" in n for n in offer["notes"])

    cph = (await search(env, "ARN", "CPH"))["offers"][0]["offer_id"]
    seats = (await tool(env, "booking_get_seat_map", {"offer_id": cph}))["structuredContent"]
    assert (
        seats["segments"][0]["free_seats_available"] == 2
        and seats["segments"][0]["cheapest_paid_seat"] == "15.00"
    )


async def test_price_change_is_spotted_before_booking(env: Env) -> None:
    offer = (await search(env, "LHR", "STN"))["offers"][0]
    fresh = await tool(
        env, "booking_get_offer", {"offer_id": offer["offer_id"], "quoted_total": offer["total_amount"]}
    )
    detail = fresh["structuredContent"]
    assert detail["price_check"] == "changed" and detail["quoted_total"] == offer["total_amount"]
    gone = (await search(env, "LGW", "LHR"))["offers"][0]["offer_id"]
    assert error_of(await tool(env, "booking_get_offer", {"offer_id": gone})) == {
        "error": "partner_conflict",
        "message": "the partner reports a conflict (the data changed)",
        "detail": "offer_no_longer_available",
    }


# ---- the write --------------------------------------------------------------------------------


async def test_happy_path_ends_confirmed_only_because_the_order_says_so(env: Env) -> None:
    offer, decision = await book(env, "ARN", "CPH")
    result = decision["result"]
    assert decision["status"] == "executed" and result["state"] == "confirmed" and result["confirmed"] is True
    order_post = json.loads(env.duffel.posts("/air/orders")[0].content)["data"]
    assert order_post["type"] == "instant" and order_post["payments"][0]["amount"] == offer["total_amount"]
    assert order_post["metadata"] == {"crewgate_booking_id": result["booking_id"]}
    assert order_post["passengers"][0]["given_name"] == "Anna"
    now = await get_booking(env, result["booking_id"])
    assert now["confirmed"] is True and now["booking_reference"] == result["booking_reference"]
    assert check_reply("Your flight is booked!", [Booking.model_validate(now)]).ok


async def test_price_change_after_approval_books_nothing(env: Env) -> None:
    offer, decision = await book(env, "LHR", "STN")
    result = decision["result"]
    assert result["state"] == "price_changed" and result["confirmed"] is False
    assert float(result["new_total"]) == float(offer["total_amount"]) + 12
    assert env.duffel.posts("/air/orders") == []
    blocked = check_reply("Done, you're booked!", [Booking.model_validate(result)])
    assert not blocked.ok and "price changed" in str(blocked.safe_reply)


async def test_timeout_after_sending_is_unknown_and_never_retried(env: Env) -> None:
    _, decision = await book(env, "ARN", "OSL")
    result = decision["result"]
    assert result["state"] == "outcome_unknown" and result["confirmed"] is False
    assert len(env.duffel.posts("/air/orders")) == 1  # the airline did book; a retry would book twice
    assert "do not book again" in result["what_to_tell_the_user"]


async def test_order_error_is_a_clear_failure(env: Env) -> None:
    _, decision = await book(env, "LHR", "LGW")
    assert (
        decision["result"]["state"] == "failed" and decision["result"]["reason"] == "invalid_order"
    )  # Duffel's code


async def test_202_is_pending_until_the_signed_webhook_settles_it(env: Env) -> None:
    _, decision = await book(env, "SEN", "STN")
    result = decision["result"]
    assert result["state"] == "pending_confirmation" and result["confirmed"] is False
    assert not check_reply("Your flight is confirmed.", [Booking.model_validate(result)]).ok

    raw = env.duffel.event("order.created", env.duffel.pending[0])
    unsigned = await env.http.post("/webhooks/duffel", content=raw)
    assert unsigned.status_code == 401
    signed = sign_webhook(SecretStr(DUFFEL_WEBHOOK_SECRET), raw, int(time.time()))
    r = await env.http.post("/webhooks/duffel", content=raw, headers={"x-duffel-signature": signed})
    assert r.status_code == 200 and r.json() == {"status": "booking_updated"}
    now = await get_booking(env, result["booking_id"])
    assert now["state"] == "confirmed" and now["confirmed"] is True
    assert (
        env.gw.metrics.value(
            "crewgate_webhooks_total", source="duffel", type="order.created", outcome="booking_updated"
        )
        == 1
    )


async def test_bookings_are_private(env: Env) -> None:
    _, decision = await book(env, "ARN", "CPH")
    other = await tool(
        env,
        "booking_get_booking",
        {"booking_id": decision["result"]["booking_id"]},
        tok=env.agent_token("erik", scope=SCOPES),
    )
    assert error_of(other)["detail"] == "booking_not_found"


async def test_the_agent_still_cannot_approve_its_own_booking(env: Env) -> None:
    offer = (await search(env, "ARN", "CPH"))["offers"][0]
    args = {
        "offer_id": offer["offer_id"],
        "expected_total": offer["total_amount"],
        "currency": "EUR",
        "market": "SE",
        "passengers": [PERSON],
    }
    pending = (await tool(env, "booking_book_flight", args))["structuredContent"]
    assert "A. Berg" in pending["summary"] and "anna@example.com" not in pending["summary"]
    r = await env.http.post(
        f"/approvals/{pending['approval_id']}/decision",
        json={"decision": "approve"},
        headers={"authorization": f"Bearer {token(env)}"},
    )
    assert r.json()["error"] == "not_a_human_sign_in" and env.duffel.posts("/air/orders") == []


@pytest.mark.parametrize(
    "bad", [{"phone_number": "0701234567"}, {"email": "not-an-email"}, {"title": "king"}]
)
async def test_passenger_details_are_validated(env: Env, bad: dict[str, str]) -> None:
    args = {
        "offer_id": "off_abc",
        "expected_total": "1.00",
        "currency": "EUR",
        "market": "SE",
        "passengers": [{**PERSON, **bad}],
    }
    assert error_of(await tool(env, "booking_book_flight", args))["error"] == "invalid_arguments"


async def test_unconfigured_booking_says_so_plainly(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = next(p for p in env.gw.plugins if p.name == "booking")
    monkeypatch.setattr(plugin.settings, "access_token", None)
    args = {"origin": "ARN", "destination": "CPH", "departure_date": DAY, "market": "SE"}
    assert error_of(await tool(env, "booking_search_flights", args)) == {
        "error": "booking_not_configured",
        "message": "this feature is not set up on this gateway (missing partner credentials)",
    }
    assert env.duffel.calls == []
