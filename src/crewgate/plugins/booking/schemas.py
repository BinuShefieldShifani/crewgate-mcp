"""Strict inputs for the booking tools. Outputs live in models.py and state.py."""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import OfferSummary

Iata = Field(pattern=r"^[A-Z]{3}$", description="IATA airport code, e.g. ARN")
Market = Field(
    pattern=r"^[A-Z]{2}$", description="Country of the customer, e.g. SE. Checked against entitlements."
)
OfferId = Field(pattern=r"^off_[A-Za-z0-9]+$", description="An offer_id from search_flights")
Amount = Field(pattern=r"^\d+(\.\d{1,2})?$", description="Exact decimal amount, e.g. 412.30")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFlightsInput(_Strict):
    origin: str = Iata
    destination: str = Iata
    departure_date: dt.date = Field(description="Day of departure (YYYY-MM-DD), today or later")
    adults: int = Field(1, ge=1, le=4)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = "economy"
    max_connections: int = Field(1, ge=0, le=2, description="0 means direct flights only")
    market: str = Market
    max_results: int = Field(5, ge=1, le=10)

    @field_validator("departure_date")
    @classmethod
    def _not_past(cls, v: dt.date) -> dt.date:
        if v < dt.date.today():
            raise ValueError("departure_date is in the past")
        return v


class OfferInput(_Strict):
    offer_id: str = OfferId
    quoted_total: str | None = Field(
        None,
        pattern=r"^\d+(\.\d{1,2})?$",
        description="The total you showed the user, to detect a price change",
    )


class SeatMapInput(_Strict):
    offer_id: str = OfferId


class PassengerInput(_Strict):
    title: Literal["mr", "ms", "mrs", "miss", "dr"]
    given_name: str = Field(min_length=1, max_length=60)
    family_name: str = Field(min_length=1, max_length=60)
    born_on: dt.date
    gender: Literal["m", "f"]
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    phone_number: str = Field(pattern=r"^\+\d{7,15}$", description="E.164, e.g. +46701234567")


class BookFlightInput(_Strict):
    offer_id: str = OfferId
    expected_total: str = Amount
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    market: str = Market
    passengers: list[PassengerInput] = Field(min_length=1, max_length=4)


class GetBookingInput(_Strict):
    booking_id: str = Field(pattern=r"^bk_[A-Za-z0-9_-]+$")


class SearchResult(BaseModel):
    offer_request_id: str
    offers: list[OfferSummary] = Field(description="Cheapest first")
    total_found: int
    message: str
