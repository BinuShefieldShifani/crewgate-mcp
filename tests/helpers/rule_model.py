"""A rule-following stand-in for a model, used to test the eval harness without spending quota.

It does what the tool descriptions ask, step by step, by reading the conversation. If every
scenario passes with it, a failing scenario with a real model means the model misbehaved, not
that the check is wrong. It is not a baseline and its numbers are never reported.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from crewgate.agent.llm import Completion, Message, ToolCall, ToolDef, Usage

PERSON = {
    "title": "ms",
    "given_name": "Anna",
    "family_name": "Berg",
    "born_on": "1990-04-02",
    "gender": "f",
    "email": "anna@example.com",
    "phone_number": "+46701234567",
}


def _call(name: str, args: dict[str, Any], n: int) -> Completion:
    return Completion(None, (ToolCall(f"rule_{n}", name, args, json.dumps(args)),), Usage(500, 20), 1.0)


def _say(text: str) -> Completion:
    return Completion(text, (), Usage(500, 40), 1.0)


class RuleModel:
    name = "rules"

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        n = len(messages)
        user_at = max(i for i, m in enumerate(messages) if m["role"] == "user")
        user = messages[user_at]["content"]
        done: list[tuple[str, dict[str, Any], dict[str, Any]]] = []  # this turn's (tool, args, result)
        calls = {}
        for m in messages[user_at:]:
            for c in m.get("tool_calls") or []:
                calls[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
            if m["role"] == "tool":
                name, args = calls[m["tool_call_id"]]
                try:
                    result = json.loads(m["content"])
                except ValueError:
                    result = {}
                done.append((name, args, result))
        names = [d[0] for d in done]
        last = done[-1] if done else None
        if last and last[2].get("error"):
            return _say(
                f"Sorry, that did not work: the airline is unavailable right now ({last[2]['error']})."
            )

        codes = re.findall(r"\(([A-Z]{3})\)", user)
        date = re.search(r"\d{4}-\d{2}-\d{2}", user)
        if "cover" in user.lower():
            if not names:
                return _call("crew_find_cover", {"date": "2026-12-05", "base": "GOT", "rank": "Captain"}, n)
            cands = last[2].get("candidates", []) if last else []
            return _say("Candidates: " + ", ".join(c["name"] for c in cands) if cands else "Nobody is free.")
        if "duty" in user.lower() and "C00" in user:
            if not names:
                ids = re.findall(r"C\d{4}", user)
                args = {
                    "requester_id": ids[0],
                    "partner_id": ids[1],
                    "date": "2026-12-06",
                    "reason": "training clash",
                }
                return _call("crew_propose_swap", args, n)
            return _say(
                f"The swap is waiting for a planner's approval: {last[2].get('approve_url') if last else ''}"
            )

        if re.search(r"approved|booked now|confirmed\?|did the booking", user, re.I):
            approval = _find(messages, r'"approval_id":\s*"([^"]+)"')
            if not names and approval:
                return _call("gateway_approval_status", {"approval_id": approval}, n)
            booking_id = _find(messages, r'"booking_id":\s*"(bk_[^"]+)"')
            if "booking_get_booking" not in names and booking_id:
                return _call("booking_get_booking", {"booking_id": booking_id}, n)
            if last and last[0] == "booking_get_booking":
                ref = last[2].get("booking_reference")
                tail = f" Reference {ref}." if last[2].get("confirmed") and ref else ""
                return _say(last[2].get("what_to_tell_the_user", "") + tail)
            return _say("I could not find that booking.")

        if len(codes) == 2 and date:
            if "booking_search_flights" not in names:
                args = {
                    "origin": codes[0],
                    "destination": codes[1],
                    "departure_date": date.group(0),
                    "market": "SE",
                }
                return _call("booking_search_flights", args, n)
            search = next(d[2] for d in done if d[0] == "booking_search_flights")
            if not search.get("offers"):
                return _say(search.get("message", "No flights found."))
            cheapest = search["offers"][0]
            if "seat" in user.lower():
                if "booking_get_seat_map" not in names:
                    return _call("booking_get_seat_map", {"offer_id": cheapest["offer_id"]}, n)
                return _say(last[2].get("message", "") if last else "")
            if "bag" in user.lower():
                bags = cheapest["checked_bags_included"]
                return _say(
                    f"It includes {bags} checked bags." if bags else "No, no checked bag is included."
                )
            if user.lower().startswith("book"):
                if "booking_get_offer" not in names:
                    args = {"offer_id": cheapest["offer_id"], "quoted_total": cheapest["total_amount"]}
                    return _call("booking_get_offer", args, n)
                offer = next(d[2] for d in done if d[0] == "booking_get_offer")
                if offer.get("price_check") == "changed":
                    return _say(
                        f"The price changed to {offer['total_amount']} {offer['currency']}. "
                        "Book at the new price?"
                    )
                if "booking_book_flight" not in names:
                    args = {
                        "offer_id": offer["offer_id"],
                        "expected_total": offer["total_amount"],
                        "currency": offer["currency"],
                        "market": "SE",
                        "passengers": [PERSON],
                    }
                    return _call("booking_book_flight", args, n)
                return _say(
                    "Done, your flight is booked! Approve it here: "
                    f"{last[2].get('approve_url') if last else ''}"
                )
            return _say(
                f"Cheapest: {cheapest['airline']} for {cheapest['total_amount']} {cheapest['currency']}."
            )
        return _say("Could you tell me more?")


def _find(messages: Sequence[Message], pattern: str) -> str | None:
    for m in reversed(messages):
        if m.get("role") == "tool" and (hit := re.search(pattern, m["content"])):
            return hit.group(1)
    return None
