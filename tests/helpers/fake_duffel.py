"""In-memory Duffel for unit tests, following the response shapes in Duffel's API docs.

Routes reproduce Duffel's own test-mode scenarios (duffel.com/docs/api/overview/test-your-integration):
  PVD-RAI  no offers                  STN-LHR  offer request times out
  BTS-MRU  no baggage reported        BTS-ABV  no extra services, and (ours) no seat map
  LHR-DXB  connecting flights         LHR-STN  price changes between search and get offer
  LGW-LHR  offer no longer available  LHR-LGW  order creation error (502)
  SEN-STN  order accepted with 202, settled later by webhook
Two extra cases the sandbox can't produce on demand:
  ARN-OSL  POST /air/orders times out AFTER the order was created (outcome unknown)
  rate_limit_next = n  the next n calls get 429 with a ratelimit-reset header
The real sandbox is checked by tests/contract/test_duffel_sandbox.py.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx

PRICES = ["412.30", "289.00", "355.50"]
DUFFEL_AIRWAYS = {"iata_code": "ZZ", "name": "Duffel Airways"}


def _err(status: int, code: str, kind: str = "airline_error") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "errors": [{"code": code, "type": kind, "title": code, "message": code, "documentation_url": ""}],
            "meta": {"request_id": uuid.uuid4().hex, "status": status},
        },
    )


@dataclass
class FakeDuffel:
    calls: list[httpx.Request] = field(default_factory=list)
    offers: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)  # offer id -> "ORG-DST"
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)  # 202 orders the webhook will announce
    rate_limit_next: int = 0

    def posts(self, path: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == "POST" and c.url.path == path]

    # ---- builders ---------------------------------------------------------------------

    def _segment(self, origin: str, dest: str, n: int, baggage: bool) -> dict[str, Any]:
        bags = [{"type": "checked", "quantity": 1}, {"type": "carry_on", "quantity": 1}] if baggage else []
        return {
            "id": f"seg_{uuid.uuid4().hex[:10]}",
            "origin": {"iata_code": origin},
            "destination": {"iata_code": dest},
            "departing_at": "2026-12-20T08:00:00",
            "arriving_at": "2026-12-20T10:30:00",
            "duration": "PT02H30M",
            "marketing_carrier": DUFFEL_AIRWAYS,
            "operating_carrier": DUFFEL_AIRWAYS,
            "marketing_carrier_flight_number": f"{100 + n}",
            "passengers": [{"passenger_id": "pas_0", "cabin_class": "economy", "baggages": bags}],
        }

    def _offer(self, route: str, price: str, adults: int) -> dict[str, Any]:
        origin, dest = route.split("-")
        segs = (
            [self._segment(origin, "DOH", 1, True), self._segment("DOH", dest, 2, True)]
            if route == "LHR-DXB"
            else [self._segment(origin, dest, 1, route != "BTS-MRU")]
        )
        oid = f"off_{uuid.uuid4().hex[:16]}"
        offer = {
            "id": oid,
            "total_amount": price,
            "total_currency": "EUR",
            "expires_at": "2026-12-01T12:00:00Z",
            "owner": DUFFEL_AIRWAYS,
            "passengers": [{"id": f"pas_{uuid.uuid4().hex[:8]}", "type": "adult"} for _ in range(adults)],
            "payment_requirements": {"requires_instant_payment": True},
            "slices": [
                {
                    "id": "sli_1",
                    "origin": {"iata_code": origin},
                    "destination": {"iata_code": dest},
                    "duration": "PT02H30M",
                    "fare_brand_name": "Basic",
                    "segments": segs,
                }
            ],
            "conditions": {
                "refund_before_departure": {"allowed": False},
                "change_before_departure": {
                    "allowed": True,
                    "penalty_amount": "50.00",
                    "penalty_currency": "EUR",
                },
            },
            "available_services": [],
        }
        self.offers[oid], self.routes[oid] = offer, route
        return offer

    def _order(
        self, offer: dict[str, Any], passengers: list[dict[str, Any]], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        oid = f"ord_{uuid.uuid4().hex[:16]}"
        order = {
            "id": oid,
            "booking_reference": uuid.uuid4().hex[:6].upper(),
            "cancelled_at": None,
            "total_amount": offer["total_amount"],
            "total_currency": offer["total_currency"],
            "payment_status": {"awaiting_payment": False, "paid_at": "2026-11-21T12:00:00Z"},
            "documents": [{"type": "electronic_ticket", "unique_identifier": "1251234567890"}],
            "passengers": passengers,
            "metadata": metadata,
            "live_mode": False,
            "created_at": "2026-11-21T12:00:00Z",
        }
        self.orders[oid] = order
        return order

    # ---- HTTP ---------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            r = _err(429, "rate_limit_exceeded", "rate_limit_error")
            r.headers["ratelimit-reset"] = str(int(time.time()) + 1)
            return r
        path, q = request.url.path, {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/air/offer_requests":
            sl = body["data"]["slices"][0]
            route = f"{sl['origin']}-{sl['destination']}"
            if route == "STN-LHR":
                raise httpx.ReadTimeout("airline took too long", request=request)
            adults = len(body["data"]["passengers"])
            offers = [] if route == "PVD-RAI" else [self._offer(route, p, adults) for p in PRICES]
            rid = f"orq_{uuid.uuid4().hex[:12]}"
            self.offers[rid] = {"ids": [o["id"] for o in offers]}
            return httpx.Response(
                201, json={"data": {"id": rid, "live_mode": False, "passengers": [], "slices": []}}
            )

        if request.method == "GET" and path == "/air/offers":
            ids = self.offers.get(q.get("offer_request_id", ""), {}).get("ids", [])
            return httpx.Response(
                200, json={"data": [self.offers[i] for i in ids], "meta": {"after": None, "limit": 50}}
            )

        if request.method == "GET" and path.startswith("/air/offers/"):
            oid = path.rsplit("/", 1)[1]
            offer, route = self.offers.get(oid), self.routes.get(oid)
            if offer is None:
                return _err(404, "not_found", "invalid_request_error")
            if route == "LGW-LHR":
                return _err(422, "offer_no_longer_available")
            if route == "LHR-STN":  # price moves between search and get offer
                offer["total_amount"] = str(Decimal(offer["total_amount"]) + Decimal("12.00"))
            services = []
            if route != "BTS-ABV" and q.get("return_available_services") == "true":
                services = [
                    {
                        "id": f"ase_{uuid.uuid4().hex[:8]}",
                        "type": "baggage",
                        "total_amount": "35.00",
                        "total_currency": "EUR",
                        "maximum_quantity": 2,
                        "passenger_ids": [p["id"] for p in offer["passengers"]],
                        "metadata": {"type": "checked", "maximum_weight_kg": 23},
                    }
                ]
            return httpx.Response(200, json={"data": {**offer, "available_services": services}})

        if request.method == "GET" and path == "/air/seat_maps":
            route = self.routes.get(q.get("offer_id", ""))
            if route in ("BTS-ABV", "BTS-MRU"):
                return httpx.Response(200, json={"data": []})
            elements: list[dict[str, Any]] = [
                {
                    "type": "seat",
                    "designator": "1A",
                    "available_services": [{"id": "ase_s1", "total_amount": "0.00", "total_currency": "EUR"}],
                },
                {
                    "type": "seat",
                    "designator": "1B",
                    "available_services": [{"id": "ase_s2", "total_amount": "0.00", "total_currency": "EUR"}],
                },
                {
                    "type": "seat",
                    "designator": "2A",
                    "available_services": [
                        {"id": "ase_s3", "total_amount": "25.00", "total_currency": "EUR"}
                    ],
                },
                {
                    "type": "seat",
                    "designator": "2B",
                    "available_services": [
                        {"id": "ase_s4", "total_amount": "15.00", "total_currency": "EUR"}
                    ],
                },
                {"type": "seat", "designator": "3A", "available_services": []},
                {"type": "lavatory"},
            ]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "sea_1",
                            "segment_id": "seg_1",
                            "cabins": [{"deck": 0, "rows": [{"sections": [{"elements": elements}]}]}],
                        }
                    ]
                },
            )

        if request.method == "POST" and path == "/air/orders":
            data = body["data"]
            offer = self.offers.get(data["selected_offers"][0])
            route = self.routes.get(data["selected_offers"][0])
            if offer is None:
                return _err(422, "offer_no_longer_available")
            if route == "LHR-LGW":
                return _err(502, "invalid_order")
            pay = data["payments"][0]
            if (
                Decimal(pay["amount"]) != Decimal(offer["total_amount"])
                or pay["currency"] != offer["total_currency"]
            ):
                return _err(422, "payment_amount_does_not_match_order_amount", "invalid_state_error")
            if {p["id"] for p in data["passengers"]} != {p["id"] for p in offer["passengers"]}:
                return _err(422, "order_passengers_incompatible_with_offer")
            if route == "SEN-STN":
                self.pending.append(self._order(offer, data["passengers"], data.get("metadata") or {}))
                return httpx.Response(202, json={})
            order = self._order(offer, data["passengers"], data.get("metadata") or {})
            if route == "ARN-OSL":  # the airline booked it, but the answer never arrived
                raise httpx.ReadTimeout("no response", request=request)
            return httpx.Response(201, json={"data": order})

        if request.method == "GET" and path.startswith("/air/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return (
                httpx.Response(200, json={"data": order})
                if order
                else _err(404, "not_found", "invalid_request_error")
            )

        return _err(404, "not_found", "invalid_request_error")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @staticmethod
    def event(event_type: str, obj: dict[str, Any], key: str | None = None) -> bytes:
        return json.dumps(
            {
                "id": f"wev_{uuid.uuid4().hex[:12]}",
                "type": event_type,
                "created_at": "2026-11-21T12:00:30Z",
                "live_mode": False,
                "idempotency_key": key or uuid.uuid4().hex,
                "object": "order",
                "data": {"object": obj},
            }
        ).encode()


class RoutingTransport(httpx.AsyncBaseTransport):
    """Send each partner's requests to its own fake, by host."""

    def __init__(self, by_host: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.by_host = by_host

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.by_host[request.url.host].handle_async_request(request)
