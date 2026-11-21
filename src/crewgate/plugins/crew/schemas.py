"""Input and output models for the crew tools. These are the JSON schemas MCP clients see.

Inputs are strict (no unknown fields, patterns on ids) so a confused or manipulated model
gets a clear validation error instead of a half-understood request reaching the partner.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Base = Literal["GOT", "ARN", "CPH", "OSL"]
Rank = Literal["Captain", "First Officer", "Purser", "Cabin Crew"]
DutyCode = Literal["FLT", "STBY", "OFF", "TRN"]
CrewId = Field(pattern=r"^C\d{4}$", description="Crew id such as C0042")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- inputs -----------------------------------------------------------------------------


class ListCrewInput(_Strict):
    base: Base | None = Field(None, description="Home base airport code, e.g. GOT for Gothenburg")
    rank: Rank | None = Field(None, description="Only this rank")
    limit: int = Field(20, ge=1, le=50, description="Maximum number of crew to return")


class GetRosterInput(_Strict):
    crew_id: str = CrewId
    from_date: dt.date | None = Field(None, description="First day to include (YYYY-MM-DD)")
    to_date: dt.date | None = Field(None, description="Last day to include (YYYY-MM-DD)")

    @model_validator(mode="after")
    def _order(self) -> "GetRosterInput":
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must not be after to_date")
        return self


class FindCoverInput(_Strict):
    date: dt.date = Field(description="Day that needs cover (YYYY-MM-DD)")
    base: Base = Field(description="Base where cover is needed")
    rank: Rank | None = Field(None, description="Rank the cover must hold")
    max_candidates: int = Field(5, ge=1, le=10)


class ProposeSwapInput(_Strict):
    requester_id: str = Field(pattern=r"^C\d{4}$", description="Crew member giving away the duty")
    partner_id: str = Field(pattern=r"^C\d{4}$", description="Crew member taking the duty")
    date: dt.date = Field(description="Day of the duty to swap (YYYY-MM-DD)")
    reason: str = Field("", max_length=200, description="Short reason shown to the approving planner")

    @model_validator(mode="after")
    def _two_people(self) -> "ProposeSwapInput":
        if self.requester_id == self.partner_id:
            raise ValueError("requester_id and partner_id must be different crew members")
        return self


# ---- outputs ----------------------------------------------------------------------------


class CrewMember(BaseModel):
    crew_id: str
    name: str
    rank: str
    base: str


class CrewList(BaseModel):
    items: list[CrewMember]
    truncated: bool = Field(description="True if more crew matched than the limit")


class Duty(BaseModel):
    date: dt.date
    duty: DutyCode
    route: str | None = None


class Roster(BaseModel):
    crew_id: str
    days: list[Duty]


class CoverCandidate(BaseModel):
    crew_id: str
    name: str
    rank: str
    duty_that_day: DutyCode


class CoverOptions(BaseModel):
    date: dt.date
    base: str
    candidates: list[CoverCandidate] = Field(description="Standby crew first, then crew who are off")
    checked: int = Field(description="How many rosters were checked")


class SwapResult(BaseModel):
    swap_id: str
    status: str
    requester_id: str
    partner_id: str
    date: dt.date
