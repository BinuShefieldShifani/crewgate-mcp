"""Offline evals: no language model, fully repeatable, run on every pull request.

  guard     the never-say-confirmed guard on a corpus of draft replies x every booking state
  security  attacks on the gateway; each must be refused with no side effect
  quirks    each Duffel quirk through the whole gateway; each must end in the documented outcome
  overhead  time the gateway adds to a tool call, partner time subtracted, p50/p95

Everything runs against the in-process gateway (tests/helpers/stack.py) with the same fakes as
the unit tests, so a number here is reproducible on any machine.
"""

import dataclasses
import datetime as dt
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from crewgate.agent.reply_guard import Facts, guard_reply
from crewgate.core.signing import sign_webhook
from crewgate.plugins.booking.state import Booking, BookingState
from tests.helpers.fake_duffel import FakeDuffel
from tests.helpers.idp import rsa_key
from tests.helpers.stack import WEBHOOK_SECRET, Stack, gateway_stack

from .common import load_jsonl, pct, percentile, wilson

DATA = Path(__file__).parent / "datasets"
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
_ids = itertools.count(1)


# ---- raw JSON-RPC helpers ------------------------------------------------------------------


async def rpc(
    st: Stack, method: str, params: dict[str, Any], token: str | None, host: str | None = None
) -> httpx.Response:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-11-25",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    if host:
        headers["host"] = host
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    return await st.http.post("/mcp", json=body, headers=headers)


async def tool(st: Stack, name: str, args: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    r = await rpc(st, "tools/call", {"name": name, "arguments": args}, token or st.agent_token())
    if r.status_code != 200:
        return {"http": r.status_code, "isError": True}
    body: dict[str, Any] = r.json()
    if "error" in body:
        return {"jsonrpc_error": body["error"].get("code"), "isError": True}
    result: dict[str, Any] = body["result"]
    return result


def body_of(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("structuredContent"):
        data: dict[str, Any] = result["structuredContent"]
        return data
    try:
        parsed: dict[str, Any] = json.loads(result["content"][0]["text"])
        return parsed
    except (KeyError, IndexError, ValueError):
        return result


async def search(st: Stack, origin: str, destination: str, token: str | None = None) -> dict[str, Any]:
    args = {"origin": origin, "destination": destination, "departure_date": DAY, "market": "SE"}
    return await tool(st, "booking_search_flights", args, token)


async def propose(st: Stack, origin: str, destination: str) -> tuple[dict[str, Any], dict[str, Any]]:
    offer = body_of(await search(st, origin, destination))["offers"][0]
    args = {
        "offer_id": offer["offer_id"],
        "expected_total": offer["total_amount"],
        "currency": offer["currency"],
        "market": "SE",
        "passengers": [PERSON],
    }
    return offer, body_of(await tool(st, "booking_book_flight", args))


async def book(st: Stack, origin: str, destination: str) -> dict[str, Any]:
    """Search, propose, the person approves. Returns the executed write's result (a Booking)."""
    _, pending = await propose(st, origin, destination)
    r = await st.decide(pending["approval_id"])
    data: dict[str, Any] = r.json()
    return data.get("result") or data


def bookings(st: Stack) -> list[Any]:
    plugin = next(p for p in st.gw.plugins if p.name == "booking")
    return list(plugin.store.all())


# ---- guard ---------------------------------------------------------------------------------

UNCONFIRMED = [s for s in BookingState if s is not BookingState.CONFIRMED]


def booking(state: BookingState) -> dict[str, Any]:
    return Booking(
        booking_id="bk_eval",
        state=state,
        booking_reference="ABC123" if state is BookingState.CONFIRMED else None,
        new_total="242.30",
        currency="EUR",
        updated_at=0,
    ).model_dump(mode="json")


def eval_guard() -> dict[str, Any]:
    drafts = load_jsonl(DATA / "guard_drafts.jsonl")
    unsafe_passed: list[dict[str, Any]] = []
    honest_blocked: list[dict[str, Any]] = []
    claim_checks = honest_checks = confirmed_blocked = 0
    for d in drafts:
        if d["kind"] == "claim":
            for state in UNCONFIRMED:  # must be blocked in every state that isn't confirmed
                claim_checks += 1
                facts = Facts({"bk_eval": booking(state)}, {}, True)
                if guard_reply(d["text"], facts).ok:
                    unsafe_passed.append({"text": d["text"], "state": state.value})
            claim_checks += 1  # and when nothing was ever booked
            if guard_reply(d["text"], Facts({}, {}, True)).ok:
                unsafe_passed.append({"text": d["text"], "state": "no_booking"})
            # a true claim must get through
            if not guard_reply(d["text"], Facts({"bk_eval": booking(BookingState.CONFIRMED)}, {}, True)).ok:
                confirmed_blocked += 1
        else:  # honest drafts: never blocked, whatever the state
            states: list[BookingState | None] = [None, *UNCONFIRMED]
            for maybe in states:
                honest_checks += 1
                held = {"bk_eval": booking(maybe)} if maybe else {}
                used = d["kind"] != "crew"
                if not guard_reply(d["text"], Facts(held, {}, used)).ok:
                    honest_blocked.append({"text": d["text"], "state": maybe.value if maybe else "none"})
    claims = sum(d["kind"] == "claim" for d in drafts)
    return {
        "drafts": len(drafts),
        "claim_drafts": claims,
        "claim_checks": claim_checks,
        "unsafe_passed": len(unsafe_passed),
        "unsafe_passed_examples": unsafe_passed[:10],
        "true_claims_blocked": confirmed_blocked,
        "honest_checks": honest_checks,
        "honest_blocked": len(honest_blocked),
        "honest_blocked_rate": len(honest_blocked) / honest_checks if honest_checks else 0.0,
        "honest_blocked_examples": honest_blocked[:10],
    }


# ---- security ------------------------------------------------------------------------------


@dataclass
class Attack:
    id: str
    what: str
    blocked: bool
    observed: str
    side_effect: bool = False


def _refused(result: dict[str, Any]) -> tuple[bool, str]:
    if "http" in result:
        return True, f"HTTP {result['http']}"
    if "jsonrpc_error" in result:
        return True, f"JSON-RPC error {result['jsonrpc_error']}"
    if result.get("isError"):
        return True, str(body_of(result).get("error"))
    return False, "allowed"


async def eval_security() -> list[Attack]:
    out: list[Attack] = []

    async def check(id: str, what: str, run: Callable[[Stack], Awaitable[tuple[bool, str, bool]]]) -> None:
        async with gateway_stack() as st:
            blocked, observed, side_effect = await run(st)
            out.append(Attack(id, what, blocked and not side_effect, observed, side_effect))

    async def call_as(
        st: Stack, token: str | None, name: str = "booking_search_flights"
    ) -> tuple[bool, str, bool]:
        args = {"origin": "ARN", "destination": "LHR", "departure_date": DAY, "market": "SE"}
        blocked, why = _refused(await tool(st, name, args, token) if token else await _anon(st, name, args))
        return blocked, why, bool(st.duffel.calls)

    async def _anon(st: Stack, name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = await rpc(st, "tools/call", {"name": name, "arguments": args}, None)
        return {"http": r.status_code, "isError": True} if r.status_code != 200 else r.json()["result"]

    await check("no-token", "call a tool with no token", lambda st: call_as(st, None))
    await check("expired", "expired access token", lambda st: call_as(st, st.agent_token(exp_in=-60)))
    await check(
        "wrong-audience",
        "token issued for another API",
        lambda st: call_as(st, st.agent_token(aud="crew-roster-api")),
    )
    await check(
        "unknown-key",
        "token signed by a key the IdP never published",
        lambda st: call_as(
            st, st.idp.token(sub="user-anna", scope="openid booking.read", key=rsa_key("evil"))
        ),
    )
    await check("alg-none", "unsigned token (alg none)", lambda st: call_as(st, _alg_none(st.agent_token())))
    await check(
        "missing-scope",
        "booking without the booking.write scope",
        lambda st: _missing_scope(st),
    )
    await check(
        "not-entitled-tool",
        "read-only dashboard client calls a booking tool",
        lambda st: call_as(st, st.agent_token(azp="roster-dashboard")),
    )
    await check("not-entitled-market", "Swedish-only client searches for market NO", _wrong_market)
    await check("self-approval", "the agent approves its own booking with its own token", _self_approval)
    await check("stale-sign-in", "approval with a sign-in older than 5 minutes", _stale_approval)
    await check("other-user-approval", "another user approves the booking", _other_user_approval)
    await check("approval-no-scope", "approval sign-in without the tool's scope", _approval_without_scope)
    await check("rate-limit", "burst of 40 calls in a minute (limit 30)", _burst)
    await check("unsigned-webhook", "Duffel webhook with no signature", _webhook("none"))
    await check("forged-webhook", "webhook signed with the wrong secret", _webhook("forged"))
    await check("replayed-webhook", "correctly signed webhook from 10 minutes ago", _webhook("old"))
    await check("bad-arguments", "path traversal in offer_id", _bad_args)
    await check("extra-field", "unknown argument smuggled into a write", _extra_field)
    await check("unknown-tool", "call a tool that does not exist", _unknown_tool)
    await check("dns-rebinding", "request with a foreign Host header", _rebinding)
    await check("cross-user-read", "read another user's booking by id", _cross_user)
    await check("write-without-human", "a write tool runs only after a person approves", _write_waits)
    return out


def _alg_none(token: str) -> str:
    import base64

    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
    return header.decode() + "." + token.split(".")[1] + "."


async def _missing_scope(st: Stack) -> tuple[bool, str, bool]:
    offer = body_of(await search(st, "ARN", "LHR"))["offers"][0]
    args = {
        "offer_id": offer["offer_id"],
        "expected_total": offer["total_amount"],
        "currency": offer["currency"],
        "market": "SE",
        "passengers": [PERSON],
    }
    blocked, why = _refused(
        await tool(st, "booking_book_flight", args, st.agent_token(scope="openid booking.read"))
    )
    return blocked, why, bool(st.duffel.posts("/air/orders"))


async def _wrong_market(st: Stack) -> tuple[bool, str, bool]:
    args = {"origin": "OSL", "destination": "LHR", "departure_date": DAY, "market": "NO"}
    blocked, why = _refused(
        await tool(st, "booking_search_flights", args, st.agent_token(azp="booking-assistant-se"))
    )
    return blocked, why, bool(st.duffel.calls)


async def _decide_with(st: Stack, token: str) -> tuple[bool, str, bool]:
    _, pending = await propose(st, "ARN", "LHR")
    r = await st.decide(pending["approval_id"], token=token)
    return (
        r.status_code >= 400,
        f"HTTP {r.status_code} {r.json().get('error', '')}",
        bool(st.duffel.posts("/air/orders")),
    )


async def _self_approval(st: Stack) -> tuple[bool, str, bool]:
    return await _decide_with(st, st.agent_token())


async def _stale_approval(st: Stack) -> tuple[bool, str, bool]:
    return await _decide_with(st, st.approval_token(auth_time=int(st.clock() - 3600)))


async def _other_user_approval(st: Stack) -> tuple[bool, str, bool]:
    return await _decide_with(st, st.approval_token(user="erik"))


async def _approval_without_scope(st: Stack) -> tuple[bool, str, bool]:
    return await _decide_with(st, st.approval_token(scope="openid"))


async def _burst(st: Stack) -> tuple[bool, str, bool]:
    token = st.agent_token()
    results = [
        await tool(st, "gateway_approval_status", {"approval_id": "apr_doesnotexist1"}, token)
        for _ in range(40)
    ]
    limited = [r for r in results if str(body_of(r).get("error", "")).startswith("rate_limited")]
    return len(limited) > 0, f"{len(limited)} of 40 refused as rate_limited", False


def _webhook(kind: str) -> Callable[[Stack], Awaitable[tuple[bool, str, bool]]]:
    async def run(st: Stack) -> tuple[bool, str, bool]:
        before = len(bookings(st))
        raw = FakeDuffel.event("order.created", {"id": "ord_eval", "booking_reference": "EVIL01"})
        headers = {"content-type": "application/json"}
        now = int(time.time())
        if kind == "forged":
            headers["x-duffel-signature"] = sign_webhook(SecretStr("guessed-secret"), raw, now)
        elif kind == "old":
            headers["x-duffel-signature"] = sign_webhook(SecretStr(WEBHOOK_SECRET), raw, now - 600)
        r = await st.http.post("/webhooks/duffel", content=raw, headers=headers)
        changed = len(bookings(st)) != before
        return r.status_code in (400, 401), f"HTTP {r.status_code} {r.json().get('error', '')}", changed

    return run


async def _bad_args(st: Stack) -> tuple[bool, str, bool]:
    blocked, why = _refused(await tool(st, "booking_get_offer", {"offer_id": "../../air/orders"}))
    return blocked, why, bool(st.duffel.calls)


async def _extra_field(st: Stack) -> tuple[bool, str, bool]:
    offer = body_of(await search(st, "ARN", "LHR"))["offers"][0]
    args = {
        "offer_id": offer["offer_id"],
        "expected_total": "0.01",
        "currency": offer["currency"],
        "market": "SE",
        "passengers": [PERSON],
        "skip_approval": True,
    }
    blocked, why = _refused(await tool(st, "booking_book_flight", args))
    return blocked, why, bool(st.duffel.posts("/air/orders"))


async def _unknown_tool(st: Stack) -> tuple[bool, str, bool]:
    blocked, why = _refused(await tool(st, "booking_delete_everything", {}))
    return blocked, why, False


async def _rebinding(st: Stack) -> tuple[bool, str, bool]:
    r = await rpc(st, "tools/list", {}, st.agent_token(), host="evil.example")
    return r.status_code == 421, f"HTTP {r.status_code}", False


async def _cross_user(st: Stack) -> tuple[bool, str, bool]:
    result = await book(st, "ARN", "LHR")
    blocked, why = _refused(
        await tool(
            st, "booking_get_booking", {"booking_id": result["booking_id"]}, st.agent_token(user="erik")
        )
    )
    return blocked, why, False


async def _write_waits(st: Stack) -> tuple[bool, str, bool]:
    _, pending = await propose(st, "ARN", "LHR")
    waited = pending.get("status") == "awaiting_approval" and not st.duffel.posts("/air/orders")
    return (
        waited,
        f"status {pending.get('status')}, orders before approval: {len(st.duffel.posts('/air/orders'))}",
        False,
    )


# ---- quirks --------------------------------------------------------------------------------


@dataclass
class Quirk:
    id: str
    what: str
    expected: str
    observed: str
    handled: bool


async def eval_quirks() -> list[Quirk]:
    out: list[Quirk] = []

    async def q(
        id: str, what: str, expected: str, run: Callable[[Stack], Awaitable[tuple[str, bool]]]
    ) -> None:
        async with gateway_stack() as st:
            observed, ok = await run(st)
            out.append(Quirk(id, what, expected, observed, ok))

    async def no_offers(st: Stack) -> tuple[str, bool]:
        data = body_of(await search(st, "PVD", "RAI"))
        return f"{data['total_found']} offers: {data['message']}", data[
            "total_found"
        ] == 0 and "No flights" in data["message"]

    async def timeout(st: Stack) -> tuple[str, bool]:
        started = time.perf_counter()
        result = await search(st, "STN", "LHR")
        secs = time.perf_counter() - started
        err = body_of(result).get("error")
        return f"{err} after {secs:.2f}s", bool(
            result.get("isError")
        ) and err == "partner_upstream_unavailable"

    async def no_bags(st: Stack) -> tuple[str, bool]:
        offer = body_of(await search(st, "BTS", "MRU"))["offers"][0]
        return f"bags {offer['checked_bags_included']}, notes {offer['notes']}", offer[
            "checked_bags_included"
        ] == 0 and bool(offer["notes"])

    async def no_seats(st: Stack) -> tuple[str, bool]:
        offer = body_of(await search(st, "BTS", "ABV"))["offers"][0]
        seats = body_of(await tool(st, "booking_get_seat_map", {"offer_id": offer["offer_id"]}))
        detail = body_of(await tool(st, "booking_get_offer", {"offer_id": offer["offer_id"]}))
        msg = seats.get("message", "")
        ok = (
            seats.get("seat_selection_available") is False
            and "check-in" in msg
            and not detail.get("extra_bags")
        )
        return f"seats: {msg}; extra bags: {len(detail.get('extra_bags') or [])}", ok

    async def connecting(st: Stack) -> tuple[str, bool]:
        offer = body_of(await search(st, "LHR", "DXB"))["offers"][0]
        stops = offer["slices"][0]["stops"]
        return f"stops {stops}", stops == 1

    async def price_change(st: Stack) -> tuple[str, bool]:
        offer = body_of(await search(st, "LHR", "STN"))["offers"][0]
        detail = body_of(
            await tool(
                st,
                "booking_get_offer",
                {"offer_id": offer["offer_id"], "quoted_total": offer["total_amount"]},
            )
        )
        result = await book(st, "LHR", "STN")
        ok = (
            detail.get("price_check") == "changed"
            and result.get("state") == "price_changed"
            and not st.duffel.posts("/air/orders")
        )
        return (
            f"get_offer: {detail.get('price_check')}; after approval: {result.get('state')}, "
            f"orders {len(st.duffel.posts('/air/orders'))}",
            ok,
        )

    async def offer_gone(st: Stack) -> tuple[str, bool]:
        result = await book(st, "LGW", "LHR")
        return f"state {result.get('state')}", result.get(
            "state"
        ) == "offer_unavailable" and not st.duffel.posts("/air/orders")

    async def order_error(st: Stack) -> tuple[str, bool]:
        result = await book(st, "LHR", "LGW")
        return f"state {result.get('state')} ({result.get('reason')})", result.get("state") == "failed"

    async def accepted_later(st: Stack) -> tuple[str, bool]:
        result = await book(st, "SEN", "STN")
        before = result.get("state")
        order = st.duffel.pending[0]
        raw = FakeDuffel.event("order.created", order)
        sig = sign_webhook(SecretStr(WEBHOOK_SECRET), raw, int(time.time()))
        await st.http.post("/webhooks/duffel", content=raw, headers={"x-duffel-signature": sig})
        st.duffel.orders[order["id"]] = order
        after = body_of(await tool(st, "booking_get_booking", {"booking_id": result["booking_id"]}))
        ok = before == "pending_confirmation" and after.get("state") == "confirmed"
        return f"{before} -> signed order.created -> {after.get('state')}", ok

    async def unknown_outcome(st: Stack) -> tuple[str, bool]:
        result = await book(st, "ARN", "OSL")
        posts = len(st.duffel.posts("/air/orders"))
        return f"state {result.get('state')}, order POSTs {posts}", result.get(
            "state"
        ) == "outcome_unknown" and posts == 1

    async def partner_429(st: Stack) -> tuple[str, bool]:
        st.duffel.rate_limit_next = 1
        result = await search(st, "ARN", "LHR")
        return (
            f"after one 429: {'ok' if not result.get('isError') else body_of(result).get('error')}",
            not result.get("isError"),
        )

    await q("no-offers", "PVD-RAI: no flights at all", "empty result with a plain message", no_offers)
    await q("supplier-timeout", "STN-LHR: the airline times out", "clear upstream error, no hang", timeout)
    await q("no-baggage", "BTS-MRU: no baggage reported", "0 bags plus a note", no_bags)
    await q("no-seat-map", "BTS-ABV: no seat map, no extras", "seats at check-in, no extra bags", no_seats)
    await q("connecting", "LHR-DXB: connecting flight", "stops = 1", connecting)
    await q(
        "price-change",
        "LHR-STN: price moves after search",
        "flagged, and nothing booked after approval",
        price_change,
    )
    await q(
        "offer-gone", "LGW-LHR: offer no longer available", "offer_unavailable, nothing booked", offer_gone
    )
    await q(
        "order-error", "LHR-LGW: airline refuses the order", "failed with the airline's code", order_error
    )
    await q(
        "accepted-later",
        "SEN-STN: 202, then the webhook",
        "pending until a signed order.created",
        accepted_later,
    )
    await q(
        "unknown-outcome",
        "ARN-OSL: timeout after the order was sent",
        "outcome_unknown, sent once, no retry",
        unknown_outcome,
    )
    await q("partner-429", "Duffel rate limits once", "retried after ratelimit-reset", partner_429)
    return out


# ---- overhead ------------------------------------------------------------------------------


class TimedTransport(httpx.AsyncBaseTransport):
    """Wraps the partner transport and records how long the partner itself took."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner, self.seconds = inner, 0.0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        started = time.perf_counter()
        try:
            return await self.inner.handle_async_request(request)
        finally:
            self.seconds += time.perf_counter() - started


async def eval_overhead(calls: int = 300) -> dict[str, Any]:
    """Per call: total time through the gateway minus time spent in the partner."""
    out: dict[str, Any] = {"calls_per_tool": calls}
    async with gateway_stack() as st:
        booking = next(p for p in st.gw.plugins if p.name == "booking")
        timed = TimedTransport(booking.ctx.partner_transport)
        for plugin in st.gw.plugins:
            if hasattr(plugin, "ctx"):
                plugin.ctx = dataclasses.replace(plugin.ctx, partner_transport=timed)
        offer = body_of(await search(st, "ARN", "LHR"))["offers"][0]
        cases = {
            "booking_get_seat_map": {"offer_id": offer["offer_id"]},
            "crew_get_roster": {"crew_id": "C0005"},
        }
        token = st.agent_token()
        for name, args in cases.items():
            overhead, totals = [], []
            for i in range(calls + 20):
                st.clock.t += 2.5  # stay under the per-user rate limit (30 a minute)
                timed.seconds = 0.0
                started = time.perf_counter()
                result = await tool(st, name, args, token)
                total = time.perf_counter() - started
                if result.get("isError"):
                    raise RuntimeError(f"{name} failed during the overhead run: {body_of(result)}")
                if i >= 20:  # warm-up: JWKS fetch, first token exchange, imports
                    totals.append(total * 1000)
                    overhead.append((total - timed.seconds) * 1000)
            out[name] = {
                "p50_ms": round(percentile(overhead, 50), 2),
                "p95_ms": round(percentile(overhead, 95), 2),
                "p99_ms": round(percentile(overhead, 99), 2),
                "total_p95_ms": round(percentile(totals, 95), 2),
            }
    return out


# ---- the whole suite -----------------------------------------------------------------------

TARGET_OVERHEAD_P95_MS = 50.0


async def run_offline(overhead_calls: int = 300) -> dict[str, Any]:
    guard = eval_guard()
    attacks = await eval_security()
    quirks = await eval_quirks()
    overhead = await eval_overhead(overhead_calls)
    blocked = sum(a.blocked for a in attacks)
    handled = sum(q.handled for q in quirks)
    worst_p95 = max(v["p95_ms"] for k, v in overhead.items() if isinstance(v, dict))
    checks = {
        "no unsafe reply passes the guard": guard["unsafe_passed"] == 0,
        "every attack refused": blocked == len(attacks),
        "every quirk handled": handled == len(quirks),
        f"gateway p95 overhead < {TARGET_OVERHEAD_P95_MS:.0f} ms": worst_p95 < TARGET_OVERHEAD_P95_MS,
    }
    return {
        "suite": "offline",
        "passed": all(checks.values()),
        "checks": checks,
        "guard": guard,
        "security": {"blocked": blocked, "total": len(attacks), "attacks": [asdict(a) for a in attacks]},
        "quirks": {"handled": handled, "total": len(quirks), "cases": [asdict(q) for q in quirks]},
        "overhead": overhead,
        "honest_blocked_ci": wilson(guard["honest_blocked"], guard["honest_checks"]),
    }


def render_offline(r: dict[str, Any]) -> str:
    g, sec, qk, ov = r["guard"], r["security"], r["quirks"], r["overhead"]
    lines = [
        "## Offline evals (no model, repeatable)",
        "",
        "| Check | Result |",
        "|---|---|",
        *[f"| {name} | {'pass' if ok else '**FAIL**'} |" for name, ok in r["checks"].items()],
        "",
        f"**Guard:** {g['claim_drafts']} claim drafts x {len(UNCONFIRMED) + 1} unconfirmed situations = "
        f"{g['claim_checks']} checks, **{g['unsafe_passed']} unsafe replies passed**. True claims wrongly "
        f"blocked: {g['true_claims_blocked']}. Honest drafts wrongly blocked: {g['honest_blocked']} of "
        f"{g['honest_checks']} ({pct(g['honest_blocked_rate'])}).",
        "",
        f"**Security:** {sec['blocked']}/{sec['total']} attacks refused with no side effect.",
        "",
        "| Attack | Refused | Observed |",
        "|---|---|---|",
        *[
            f"| {a['what']} | {'yes' if a['blocked'] else '**NO**'} | {a['observed']} |"
            for a in sec["attacks"]
        ],
        "",
        f"**Duffel quirks:** {qk['handled']}/{qk['total']} handled as documented.",
        "",
        "| Quirk | Expected | Observed | OK |",
        "|---|---|---|---|",
        *[
            f"| {q['what']} | {q['expected']} | {q['observed']} | {'yes' if q['handled'] else '**NO**'} |"
            for q in qk["cases"]
        ],
        "",
        f"**Gateway overhead** ({ov['calls_per_tool']} calls per tool, partner time subtracted, in-process):",
        "",
        "| Tool | p50 | p95 | p99 |",
        "|---|---|---|---|",
        *[
            f"| {k} | {v['p50_ms']} ms | {v['p95_ms']} ms | {v['p99_ms']} ms |"
            for k, v in ov.items()
            if isinstance(v, dict)
        ],
    ]
    return "\n".join(lines) + "\n"
