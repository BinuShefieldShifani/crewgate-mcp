"""Step 5 demo: book a flight on Duffel's real test API through the crewgate MCP gateway.

  1. sign in, search, check the price again, look at seats and bags
  2. propose a booking -> awaiting approval; the agent can't approve it itself
  3. the person approves -> the gateway re-checks the price and books once
  4. the booking state comes from the order; the "never say confirmed" guard agrees with it
  5. Duffel's price-change scenario: approved, but nothing is booked
  6. webhooks: unsigned refused, signed accepted

Needs the stack (make up) started with DUFFEL_ACCESS_TOKEN set to a duffel_test_ token.
    make demo-booking
"""

import asyncio
import datetime as dt
import itertools
import json
import time
from typing import Any

import httpx
from pydantic import SecretStr

from crewgate.clientauth import discover_authorization_server
from crewgate.clientauth.devlogin import headless_login
from crewgate.core.signing import sign_webhook
from crewgate.plugins.booking.confirmation import check_reply
from crewgate.plugins.booking.state import Booking

GATEWAY = "http://localhost:8000"
ISSUER = "http://localhost:8080/realms/crewgate"
DAY = (dt.date.today() + dt.timedelta(days=60)).isoformat()
PASSENGER = {
    "title": "ms",
    "given_name": "Anna",
    "family_name": "Testsson",
    "born_on": "1990-04-02",
    "gender": "f",
    "email": "anna.testsson@example.com",
    "phone_number": "+46701234567",
}
_ids = itertools.count(1)


async def main() -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=60.0) as http:
        server = await discover_authorization_server(http, ISSUER)

        async def sign_in(
            client_id: str, scopes: list[str], secret: str | None = None, redirect: str = ""
        ) -> str:
            t = await headless_login(
                http,
                server,
                client_id=client_id,
                client_secret=secret,
                username="anna",
                password="dev-only-anna",
                redirect_uri=redirect or "http://127.0.0.1:8765/callback",
                scopes=scopes,
            )
            return str(t["access_token"])

        agent = await sign_in("crewgate-mcp-client", ["openid", "booking.read", "booking.write"])

        async def tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            r = await http.post(
                f"{GATEWAY}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": next(_ids),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
                headers={"authorization": f"Bearer {agent}", "accept": "application/json, text/event-stream"},
            )
            result: dict[str, Any] = r.json()["result"]
            if result["isError"]:
                raise SystemExit(f"{name} failed: {result['content'][0]['text']}")
            data: dict[str, Any] = result["structuredContent"]
            return data

        async def approve(approval_id: str) -> dict[str, Any]:
            human = await sign_in(
                "crewgate-approvals",
                ["openid", "booking.write"],
                "dev-only-approvals-secret",
                f"{GATEWAY}/approvals/callback",
            )
            r = await http.post(
                f"{GATEWAY}/approvals/{approval_id}/decision",
                json={"decision": "approve"},
                headers={"authorization": f"Bearer {human}"},
            )
            body: dict[str, Any] = r.json()
            return body

        found = await tool(
            "booking_search_flights",
            {"origin": "LHR", "destination": "JFK", "departure_date": DAY, "market": "SE"},
        )
        print(f"1. {found['message']}")
        for o in found["offers"][:3]:
            print(
                f"   {o['offer_id'][:18]}..  {o['airline']:<18} {o['total_amount']:>8} {o['currency']}  "
                f"bags {o['checked_bags_included']}  stops {o['slices'][0]['stops']}"
            )
        pick = next((o for o in found["offers"] if o["airline"] == "Duffel Airways"), found["offers"][0])
        fresh = await tool(
            "booking_get_offer", {"offer_id": pick["offer_id"], "quoted_total": pick["total_amount"]}
        )
        seats = await tool("booking_get_seat_map", {"offer_id": pick["offer_id"]})
        print(
            f"   price check: {fresh['price_check']}; extra bags for sale: {len(fresh['extra_bags'])}; "
            f"seats: {seats['message']}"
        )

        args = {
            "offer_id": pick["offer_id"],
            "expected_total": fresh["total_amount"],
            "currency": fresh["currency"],
            "market": "SE",
            "passengers": [PASSENGER],
        }
        pending = await tool("booking_book_flight", args)
        print(f"2. booking_book_flight -> {pending['status']}: {pending['summary']}")
        r = await http.post(
            f"{GATEWAY}/approvals/{pending['approval_id']}/decision",
            json={"decision": "approve"},
            headers={"authorization": f"Bearer {agent}"},
        )
        print(f"   agent approves itself -> HTTP {r.status_code} {r.json()['error']}")

        decision = await approve(pending["approval_id"])
        result = decision.get("result") or {}
        print(
            f"3. person approves -> {decision.get('status')}: state={result.get('state')} "
            f"ref={result.get('booking_reference')} e-tickets={result.get('e_tickets')}"
        )
        if result.get("booking_id"):
            booking = await tool("booking_get_booking", {"booking_id": result["booking_id"]})
            verdict = check_reply("Your flight is booked!", [Booking.model_validate(booking)])
            said = "allowed" if verdict.ok else f"BLOCKED -> {verdict.safe_reply}"
            print(
                f"4. booking_get_booking -> confirmed={booking['confirmed']}; "
                f"draft 'Your flight is booked!' {said}"
            )

        moving = (
            await tool(
                "booking_search_flights",
                {"origin": "LHR", "destination": "STN", "departure_date": DAY, "market": "SE"},
            )
        )["offers"][0]
        pending = await tool(
            "booking_book_flight",
            {
                **args,
                "offer_id": moving["offer_id"],
                "expected_total": moving["total_amount"],
                "currency": moving["currency"],
            },
        )
        changed = (await approve(pending["approval_id"])).get("result") or {}
        print(
            f"5. Duffel's price-change route LHR-STN -> state={changed.get('state')}, new total "
            f"{changed.get('new_total')}: {changed.get('what_to_tell_the_user')}"
        )

        ping = json.dumps(
            {
                "id": "wev_demo",
                "type": "ping.triggered",
                "idempotency_key": f"demo-{time.time()}",
                "live_mode": False,
                "data": {"object": {}},
            }
        ).encode()
        unsigned = await http.post(f"{GATEWAY}/webhooks/duffel", content=ping)
        signature = sign_webhook(SecretStr("dev-only-duffel-webhook-secret"), ping, int(time.time()))
        signed = await http.post(
            f"{GATEWAY}/webhooks/duffel", content=ping, headers={"x-duffel-signature": signature}
        )
        print(
            f"6. webhook unsigned -> {unsigned.status_code} {unsigned.json()['error']}; "
            f"signed -> {signed.status_code} {signed.json()['status']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
