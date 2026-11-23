"""Duffel's nested JSON turned into small, flat answers an agent can use without misreading.

Every quirk becomes an explicit field instead of a silent gap:
- no baggage data from the airline   -> checked_bags_included=0 plus a note saying so
- no extra bags for sale             -> extra_bags=[] plus a note
- no seat map                        -> seat_selection_available=False plus a note
Parsing is defensive: a missing field never crashes a tool, it becomes None or a note.
"""

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field


class SegmentView(BaseModel):
    flight: str = Field(description="Marketing carrier code + number, e.g. BA1234")
    origin: str
    destination: str
    departing_at: str | None = None
    arriving_at: str | None = None
    operated_by: str | None = None


class SliceView(BaseModel):
    origin: str
    destination: str
    duration: str | None = None
    fare_brand: str | None = None
    stops: int
    segments: list[SegmentView]


class OfferSummary(BaseModel):
    offer_id: str
    airline: str | None = None
    total_amount: str = Field(description="Exact price as a decimal string, e.g. '412.30'")
    currency: str
    expires_at: str | None = None
    slices: list[SliceView]
    checked_bags_included: int = Field(
        description="Checked bags included per passenger (lowest across flights)"
    )
    carry_on_bags_included: int
    refundable: bool | None = None
    changeable: bool | None = None
    notes: list[str] = Field(default_factory=list)


class ExtraBag(BaseModel):
    service_id: str
    kind: str
    max_weight_kg: float | None = None
    price: str
    currency: str
    max_quantity: int | None = None


class PassengerSlot(BaseModel):
    passenger_id: str
    type: str | None = None


class OfferDetail(OfferSummary):
    passengers: list[PassengerSlot]
    extra_bags: list[ExtraBag]
    requires_instant_payment: bool | None = None
    price_check: str = Field(
        "not_checked", description="unchanged, changed or not_checked against quoted_total"
    )
    quoted_total: str | None = None


class SegmentSeats(BaseModel):
    segment_id: str | None = None
    seats_total: int
    seats_available: int
    free_seats_available: int
    cheapest_paid_seat: str | None = None
    currency: str | None = None


class SeatMapSummary(BaseModel):
    offer_id: str
    seat_selection_available: bool
    segments: list[SegmentSeats]
    message: str


def _code(place: Any) -> str:
    return str((place or {}).get("iata_code") or "?")


def _bags(segment_passenger: dict[str, Any], kind: str) -> int:
    return sum(
        int(b.get("quantity") or 0) for b in segment_passenger.get("baggages") or [] if b.get("type") == kind
    )


def summarize_offer(offer: dict[str, Any]) -> OfferSummary:
    notes: list[str] = []
    slices: list[SliceView] = []
    checked: list[int] = []
    carry: list[int] = []
    baggage_reported = False
    for sl in offer.get("slices") or []:
        segments = []
        for seg in sl.get("segments") or []:
            carrier = _code(seg.get("marketing_carrier"))
            number = seg.get("marketing_carrier_flight_number") or ""
            operator = _code(seg.get("operating_carrier"))
            segments.append(
                SegmentView(
                    flight=f"{carrier}{number}",
                    origin=_code(seg.get("origin")),
                    destination=_code(seg.get("destination")),
                    departing_at=seg.get("departing_at"),
                    arriving_at=seg.get("arriving_at"),
                    operated_by=operator if operator not in (carrier, "?") else None,
                )
            )
            for sp in seg.get("passengers") or []:
                if sp.get("baggages"):
                    baggage_reported = True
                checked.append(_bags(sp, "checked"))
                carry.append(_bags(sp, "carry_on"))
        slices.append(
            SliceView(
                origin=_code(sl.get("origin")),
                destination=_code(sl.get("destination")),
                duration=sl.get("duration"),
                fare_brand=sl.get("fare_brand_name"),
                stops=max(0, len(segments) - 1),
                segments=segments,
            )
        )
    checked_included = min(checked) if checked else 0
    if not baggage_reported:
        notes.append("The airline did not report any included baggage: assume no bags are included.")
    elif checked_included == 0:
        notes.append("No checked bag is included in this fare.")
    conditions = offer.get("conditions") or {}

    def allowed(key: str) -> bool | None:
        c = conditions.get(key)
        return bool(c.get("allowed")) if isinstance(c, dict) and "allowed" in c else None

    return OfferSummary(
        offer_id=str(offer["id"]),
        airline=(offer.get("owner") or {}).get("name"),
        total_amount=str(offer.get("total_amount")),
        currency=str(offer.get("total_currency")),
        expires_at=offer.get("expires_at"),
        slices=slices,
        checked_bags_included=checked_included,
        carry_on_bags_included=min(carry) if carry else 0,
        refundable=allowed("refund_before_departure"),
        changeable=allowed("change_before_departure"),
        notes=notes,
    )


def same_price(a: str | None, b: str | None) -> bool:
    try:
        return a is not None and b is not None and Decimal(a) == Decimal(b)
    except InvalidOperation:
        return False


def detail_offer(offer: dict[str, Any], quoted_total: str | None = None) -> OfferDetail:
    base = summarize_offer(offer)
    extra = [
        ExtraBag(
            service_id=str(s["id"]),
            kind=str((s.get("metadata") or {}).get("type") or "bag"),
            max_weight_kg=(s.get("metadata") or {}).get("maximum_weight_kg"),
            price=str(s.get("total_amount")),
            currency=str(s.get("total_currency")),
            max_quantity=s.get("maximum_quantity"),
        )
        for s in offer.get("available_services") or []
        if s.get("type") == "baggage" and s.get("id")
    ]
    notes = list(base.notes)
    if not extra:
        notes.append("The airline offers no extra bags to buy for this fare.")
    price_check = "not_checked"
    if quoted_total is not None:
        price_check = "unchanged" if same_price(quoted_total, base.total_amount) else "changed"
        if price_check == "changed":
            notes.append(
                f"Price changed from {quoted_total} to {base.total_amount} {base.currency} since search."
            )
    return OfferDetail(
        **base.model_dump(exclude={"notes"}),
        notes=notes,
        passengers=[
            PassengerSlot(passenger_id=str(p["id"]), type=p.get("type"))
            for p in offer.get("passengers") or []
        ],
        extra_bags=extra,
        requires_instant_payment=(offer.get("payment_requirements") or {}).get("requires_instant_payment"),
        price_check=price_check,
        quoted_total=quoted_total,
    )


def summarize_seat_maps(offer_id: str, maps: list[dict[str, Any]]) -> SeatMapSummary:
    if not maps:
        return SeatMapSummary(
            offer_id=offer_id,
            seat_selection_available=False,
            segments=[],
            message=(
                "The airline does not offer seat selection for this flight. Seats are assigned at check-in."
            ),
        )
    segments: list[SegmentSeats] = []
    for m in maps:
        total = available = free = 0
        cheapest: Decimal | None = None
        currency = None
        for cabin in m.get("cabins") or []:
            for row in cabin.get("rows") or []:
                for section in row.get("sections") or []:
                    for el in section.get("elements") or []:
                        if el.get("type") != "seat":
                            continue
                        total += 1
                        services = el.get("available_services") or []
                        if services:
                            available += 1
                        for svc in services:
                            try:
                                price = Decimal(str(svc.get("total_amount")))
                            except InvalidOperation:
                                continue
                            if price == 0:
                                free += 1
                            elif cheapest is None or price < cheapest:
                                cheapest, currency = price, svc.get("total_currency")
        segments.append(
            SegmentSeats(
                segment_id=m.get("segment_id"),
                seats_total=total,
                seats_available=available,
                free_seats_available=free,
                cheapest_paid_seat=str(cheapest) if cheapest is not None else None,
                currency=currency,
            )
        )
    return SeatMapSummary(
        offer_id=offer_id,
        seat_selection_available=any(s.seats_available for s in segments),
        segments=segments,
        message="Seat selection is available."
        if any(s.seats_available for s in segments)
        else "No seats are free.",
    )
