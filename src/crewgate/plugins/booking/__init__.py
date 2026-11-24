"""Booking plugin: flight search and booking on Duffel's test-mode API (Etraveli use case).

Tools:
  booking.search_flights  read    cheapest offers, bags and conditions spelled out
  booking.get_offer       read    fresh price (catches price changes), passenger slots, extra bags
  booking.get_seat_map    read    whether seat selection exists at all, and what it costs
  booking.book_flight     WRITE   needs human approval; re-checks the price before booking
  booking.get_booking     read    the state derived from the order: the only source for "confirmed"

The caller is insulated from Duffel's quirks: every one becomes a field, a note or a clear state.
"""

from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from crewgate.core.auth import Principal
from crewgate.core.descriptions import check_table, describe
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.plugin import PluginContext, ToolSpec

from .config import DuffelSettings
from .duffel import DuffelClient
from .models import (
    OfferDetail,
    SeatMapSummary,
    detail_offer,
    same_price,
    summarize_offer,
    summarize_seat_maps,
)
from .schemas import (
    BookFlightInput,
    GetBookingInput,
    OfferInput,
    SearchFlightsInput,
    SearchResult,
    SeatMapInput,
)
from .state import Booking, BookingState, BookingStore, from_order
from .webhooks import DuffelWebhooks

DESCRIPTIONS: dict[str, dict[str, str]] = {
    "minimal": {
        "booking.search_flights": "Search flights.",
        "booking.get_offer": "Get an offer.",
        "booking.get_seat_map": "Get the seat map for an offer.",
        "booking.book_flight": "Book a flight.",
        "booking.get_booking": "Get a booking.",
    },
    "guided": {
        "booking.search_flights": (
            "Search one-way flights between two airports on a date. Returns the cheapest offers with exact "
            "prices, stops, included bags and whether the fare is refundable. Prices can change: before "
            "booking, call booking_get_offer with quoted_total to check the price is still the same."
        ),
        "booking.get_offer": (
            "Get the current price and details of one offer: passenger slots, included and extra bags, "
            "refund and change rules. Pass quoted_total (the price you showed the user) to learn whether "
            "the price changed. Always do this right before proposing a booking. If the price changed, "
            "tell the user the new price and wait for their OK before booking_book_flight."
        ),
        "booking.get_seat_map": (
            "Check whether seats can be chosen for an offer and what seats cost. Some airlines offer no "
            "seat selection at all: then say seats are assigned at check-in."
        ),
        "booking.book_flight": (
            "Propose booking an offer for named passengers at an exact price. This does NOT book anything. "
            "It creates a request a person must approve in their browser, and the gateway checks the price "
            "again before booking. Never tell the user the flight is booked or confirmed; use "
            "booking_get_booking and only say confirmed when its 'confirmed' field is true."
        ),
        "booking.get_booking": (
            "Get the real state of a booking attempt: confirmed, pending_confirmation, awaiting_payment, "
            "outcome_unknown, price_changed, offer_unavailable, cancelled or failed. Its "
            "what_to_tell_the_user field is the safe thing to say. Only 'confirmed' means booked."
        ),
    },
    "contract": {
        "booking.search_flights": (
            "Search one-way flights for a route and a date.\n"
            "Use when: the user wants flight options, prices or times between two airports.\n"
            "Not for: re-checking one offer the user already picked (booking_get_offer), seats "
            "(booking_get_seat_map), booking (booking_book_flight).\n"
            "Needs: origin and destination as IATA codes (ARN, GOT, LHR), departure_date YYYY-MM-DD, "
            "market = the customer's country (SE, NO, DK, FI).\n"
            "Returns: offers cheapest first, each with offer_id, total_amount, currency, stops, "
            "included bags and refundability.\n"
            "Next: before proposing a booking, booking_get_offer with quoted_total."
        ),
        "booking.get_offer": (
            "Fresh price and details of one offer_id from a search.\n"
            "Use when: the user picked an offer, asks about its bags, refund or change rules, or wants "
            "to book it.\n"
            "Not for: finding flights (booking_search_flights), seats (booking_get_seat_map).\n"
            "Needs: offer_id; quoted_total = the price you showed the user.\n"
            "Returns: price_check unchanged or changed, passenger slots, included and extra bags.\n"
            "Next: if the price changed, tell the user the new price and wait for their OK before "
            "booking_book_flight."
        ),
        "booking.get_seat_map": (
            "Seat selection for one offer_id.\n"
            "Use when: the user asks about choosing seats, seat prices or where they will sit.\n"
            "Not for: bags or price (booking_get_offer).\n"
            "Needs: offer_id.\n"
            "Returns: whether seats can be chosen and seat prices. No seat map means seats are "
            "assigned at check-in."
        ),
        "booking.book_flight": (
            "Propose a booking. It does NOT book: it creates a request a person approves in their browser.\n"
            "Use when: the user clearly asks to book a specific offer and you have every passenger's "
            "details.\n"
            "Not for: checking a price (booking_get_offer) or the state of a booking (booking_get_booking).\n"
            "Needs: offer_id, expected_total and currency exactly as booking_get_offer returned them, "
            "market, passengers.\n"
            "Returns: status awaiting_approval and an approve_url. Nothing is booked yet.\n"
            "Next: tell the user it awaits their approval. Never say booked or confirmed."
        ),
        "booking.get_booking": (
            "The real state of a booking attempt, read from the airline order.\n"
            "Use when: the user asks whether a flight is booked, confirmed or ticketed, or for its "
            "reference, and you have a booking_id.\n"
            "Not for: a request still awaiting approval with no booking_id yet "
            "(gateway_approval_status).\n"
            "Needs: booking_id (bk_...).\n"
            "Returns: state, confirmed (true only when booked, paid and ticketed), what_to_tell_the_user.\n"
            "Next: say exactly what what_to_tell_the_user says."
        ),
    },
}


class BookingPlugin:
    name = "booking"

    def __init__(self, ctx: PluginContext, settings: DuffelSettings | None = None) -> None:
        self.ctx = ctx
        self.settings = settings or DuffelSettings()
        self.store = BookingStore()
        self.webhooks = DuffelWebhooks(self.settings, self.store, ctx)

    def _client(self) -> DuffelClient:
        return DuffelClient(self.settings, transport=self.ctx.partner_transport, metrics=self.ctx.metrics)

    # ---- reads ------------------------------------------------------------------------------

    async def search_flights(
        self, p: Principal, *, market: str, max_results: int = 5, **query: Any
    ) -> SearchResult:
        async with self._client() as duffel:
            offer_request, offers = await duffel.search(**query)
        summaries = sorted((summarize_offer(o) for o in offers), key=lambda o: float(o.total_amount))
        message = (
            f"{len(summaries)} offers found, cheapest first."
            if summaries
            else "No flights found for this route and date. Try another date or allow more connections."
        )
        return SearchResult(
            offer_request_id=str(offer_request["id"]),
            offers=summaries[:max_results],
            total_found=len(summaries),
            message=message,
        )

    async def get_offer(self, p: Principal, *, offer_id: str, quoted_total: str | None = None) -> OfferDetail:
        async with self._client() as duffel:
            return detail_offer(await duffel.get_offer(offer_id), quoted_total)

    async def get_seat_map(self, p: Principal, *, offer_id: str) -> SeatMapSummary:
        async with self._client() as duffel:
            return summarize_seat_maps(offer_id, await duffel.seat_maps(offer_id))

    async def get_booking(self, p: Principal, *, booking_id: str) -> Booking:
        booking = self.store.get(booking_id, subject=p.subject)
        if booking is None:
            raise PartnerError(ErrorCategory.NOT_FOUND, "booking_not_found", code="booking_not_found")
        if booking.order_id and booking.state is not BookingState.CANCELLED:
            async with self._client() as duffel:  # never trust a cached "confirmed": ask the order
                order = await duffel.get_order(booking.order_id)
            booking = self.store.update_from_order(booking_id, order) or booking
        return booking

    # ---- the write --------------------------------------------------------------------------

    async def book_flight(
        self,
        p: Principal,
        *,
        offer_id: str,
        expected_total: str,
        currency: str,
        market: str,
        passengers: list[Any],
    ) -> Booking:
        """Runs only after a person approved it. Re-checks the price, then books once."""
        booking_id = self.store.new_id()

        def outcome(state: BookingState, **fields: Any) -> Booking:
            return self.store.put(
                p.subject,
                Booking(booking_id=booking_id, state=state, updated_at=int(self.store.clock()), **fields),
            )

        async with self._client() as duffel:
            try:
                offer = await duffel.get_offer(offer_id)
            except PartnerError as exc:
                if exc.category in (ErrorCategory.CONFLICT, ErrorCategory.NOT_FOUND):
                    return outcome(BookingState.OFFER_UNAVAILABLE, reason=exc.code or exc.message)
                raise
            total, cur = str(offer.get("total_amount")), str(offer.get("total_currency"))
            if not same_price(total, expected_total) or cur != currency:
                return outcome(
                    BookingState.PRICE_CHANGED, new_total=total, total_amount=expected_total, currency=cur
                )
            slots = [str(x["id"]) for x in offer.get("passengers") or []]
            if len(slots) != len(passengers):
                return outcome(
                    BookingState.FAILED, reason=f"offer is for {len(slots)} passengers, got {len(passengers)}"
                )
            people = [
                {**_plain(person), "id": slot, "born_on": str(_plain(person)["born_on"])}
                for slot, person in zip(slots, passengers, strict=True)
            ]
            try:
                order = await duffel.create_order(
                    offer_id=offer_id,
                    passengers=people,
                    amount=total,
                    currency=cur,
                    metadata={"crewgate_booking_id": booking_id},
                )
            except PartnerError as exc:
                if exc.category is ErrorCategory.OUTCOME_UNKNOWN:
                    return outcome(
                        BookingState.OUTCOME_UNKNOWN, total_amount=total, currency=cur, reason=exc.message
                    )
                if exc.code == "price_changed":
                    return outcome(
                        BookingState.PRICE_CHANGED, total_amount=total, currency=cur, reason=exc.code
                    )
                if exc.code in ("offer_no_longer_available", "offer_expired"):
                    return outcome(BookingState.OFFER_UNAVAILABLE, reason=exc.code)
                return outcome(BookingState.FAILED, reason=exc.code or exc.category.value)
        if order is None:  # 202: accepted, the order.created / order.creation_failed webhook will settle it
            return outcome(
                BookingState.PENDING_CONFIRMATION,
                total_amount=total,
                currency=cur,
                reason="accepted_not_created",
            )
        return self.store.put(p.subject, from_order(booking_id, order, self.store.clock))

    # ---- wiring -----------------------------------------------------------------------------

    def describe(self, tool: str) -> str:
        return describe(DESCRIPTIONS, tool, self.ctx.description_variant)

    def routes(self) -> list[Route]:
        async def receive(request: Request) -> Response:
            status, body = await self.webhooks.handle(
                request.headers.get("x-duffel-signature"), await request.body()
            )
            return JSONResponse(body, status)

        return [Route("/webhooks/duffel", receive, methods=["POST"])]

    def tools(self) -> list[ToolSpec]:
        def summarize_booking(a: Mapping[str, Any]) -> str:
            people = a.get("passengers") or []
            names = ", ".join(f"{_plain(x)['given_name'][:1]}. {_plain(x)['family_name']}" for x in people)
            return (
                f"Book offer {a['offer_id']} for {len(people)} passenger(s) ({names}) "
                f"for exactly {a['expected_total']} {a['currency']}. "
                "The price is checked again before booking."
            )

        specs = [
            ToolSpec(
                "booking.search_flights",
                self.describe("booking.search_flights"),
                "booking.read",
                self.search_flights,
                title="Search flights",
                input_model=SearchFlightsInput,
                output_model=SearchResult,
                market_bound=True,
                idempotent=True,
            ),
            ToolSpec(
                "booking.get_offer",
                self.describe("booking.get_offer"),
                "booking.read",
                self.get_offer,
                title="Check an offer",
                input_model=OfferInput,
                output_model=OfferDetail,
                idempotent=True,
            ),
            ToolSpec(
                "booking.get_seat_map",
                self.describe("booking.get_seat_map"),
                "booking.read",
                self.get_seat_map,
                title="Seat map",
                input_model=SeatMapInput,
                output_model=SeatMapSummary,
                idempotent=True,
            ),
            ToolSpec(
                "booking.book_flight",
                self.describe("booking.book_flight"),
                "booking.write",
                self.book_flight,
                title="Propose a booking",
                input_model=BookFlightInput,
                output_model=Booking,
                writes=True,
                market_bound=True,
                summarize=summarize_booking,
            ),
            ToolSpec(
                "booking.get_booking",
                self.describe("booking.get_booking"),
                "booking.read",
                self.get_booking,
                title="Booking status",
                input_model=GetBookingInput,
                output_model=Booking,
                idempotent=True,
            ),
        ]
        check_table(DESCRIPTIONS, [s.name for s in specs])
        return specs


def _plain(x: Any) -> dict[str, Any]:
    return x.model_dump() if hasattr(x, "model_dump") else dict(x)


def build(ctx: PluginContext | None = None) -> BookingPlugin:
    return BookingPlugin(ctx or PluginContext())
