"""Crew plugin: airline crew rosters and duty swaps (Wise IT use case).

Every partner call is made on behalf of the signed-in planner (OBO), HMAC-signed, and the
partner checks the planner's own roles. Swaps are write tools: they need human approval.
"""

from collections.abc import Mapping
from typing import Any

from crewgate.core.auth import OnBehalfOf, Principal
from crewgate.core.descriptions import check_table, describe
from crewgate.core.plugin import PluginContext, ToolSpec

from .client import RosterClient
from .config import CrewSettings
from .schemas import (
    CoverCandidate,
    CoverOptions,
    CrewList,
    CrewMember,
    FindCoverInput,
    GetRosterInput,
    ListCrewInput,
    ProposeSwapInput,
    Roster,
    SwapResult,
)

AUDIENCE = "crew-roster-api"
_COVER_ORDER = {"STBY": 0, "OFF": 1}
MAX_ROSTERS_PER_COVER_SEARCH = 25  # each roster is one partner call; keep one tool call cheap

DESCRIPTIONS: dict[str, dict[str, str]] = {
    "minimal": {
        "crew.list_crew": "List crew.",
        "crew.get_roster": "Get a roster.",
        "crew.find_cover": "Find cover.",
        "crew.propose_swap": "Swap a duty.",
    },
    "guided": {
        "crew.list_crew": (
            "List crew members, optionally filtered by home base and rank. Use it to find crew ids "
            "before reading a roster. Returns names, ranks and bases only, never contact details."
        ),
        "crew.get_roster": (
            "Read one crew member's duties day by day (FLT flight, STBY standby, OFF day off, TRN "
            "training). Needs a crew id such as C0042; use crew_list_crew first if you only have a name."
        ),
        "crew.find_cover": (
            "Find crew who could cover a duty on a given day at a base: standby crew first, then crew "
            "who are off. Read-only: it does not book or change anything."
        ),
        "crew.propose_swap": (
            "Propose swapping one crew member's duty on a day to another crew member. This does NOT "
            "make the swap. It creates a request that a planner must approve in their browser; until "
            "then nothing changes. Tell the user it is awaiting approval, never that it is done."
        ),
    },
    "contract": {
        "crew.list_crew": (
            "Crew members with id, name, rank and home base.\n"
            "Use when: you need a crew_id for a name, or the user asks who is based somewhere or holds "
            "a rank.\n"
            "Not for: duties or schedules (crew_get_roster), who can cover a day (crew_find_cover).\n"
            "Needs: nothing; optional base (GOT, ARN, CPH, OSL) and rank.\n"
            "Returns: names, ranks and bases only, never contact details."
        ),
        "crew.get_roster": (
            "One crew member's duties day by day: FLT flight, STBY standby, OFF day off, TRN training.\n"
            "Use when: the user asks what someone is doing, whether they work, or their schedule.\n"
            "Not for: finding a replacement (crew_find_cover).\n"
            "Needs: crew_id like C0042. Only a name? Call crew_list_crew first. Optional from_date, "
            "to_date (YYYY-MM-DD)."
        ),
        "crew.find_cover": (
            "Crew who could take a duty on a day at a base: standby crew first, then crew who are off.\n"
            "Use when: someone is sick or unavailable and the user needs a replacement.\n"
            "Not for: making the change (crew_propose_swap).\n"
            "Needs: date (YYYY-MM-DD), base (GOT, ARN, CPH, OSL), optional rank.\n"
            "Returns: candidates. Read-only: nothing changes."
        ),
        "crew.propose_swap": (
            "Propose giving one crew member's duty on a day to another. It does NOT make the swap: a "
            "planner approves it in their browser.\n"
            "Use when: the user asks to swap, hand over or reassign a duty between two named crew.\n"
            "Not for: looking for who could cover (crew_find_cover).\n"
            "Needs: requester_id (gives the duty away), partner_id (takes it), date, short reason.\n"
            "Returns: status awaiting_approval. Tell the user it awaits approval, never that it is done."
        ),
    },
}


class CrewPlugin:
    name = "crew"

    def __init__(self, ctx: PluginContext, settings: CrewSettings | None = None) -> None:
        self.ctx, self.settings = ctx, settings or CrewSettings()

    def _client(self, principal: Principal, scopes: list[str]) -> RosterClient:
        if self.ctx.exchanger is None:
            raise RuntimeError("crew plugin needs a token exchanger for on-behalf-of calls")
        auth = OnBehalfOf(self.ctx.exchanger, principal, AUDIENCE, scopes)
        return RosterClient(
            self.settings, transport=self.ctx.partner_transport, auth=auth, metrics=self.ctx.metrics
        )

    async def list_crew(
        self, p: Principal, base: str | None = None, rank: str | None = None, limit: int = 20
    ) -> CrewList:
        items: list[CrewMember] = []
        truncated = False
        async with self._client(p, ["roster.read"]) as client:
            async for c in client.iter_crew(page_size=50):
                if (base and c["base"] != base) or (rank and c["rank"] != rank):
                    continue
                if len(items) == limit:
                    truncated = True
                    break
                items.append(CrewMember(crew_id=c["crew_id"], name=c["name"], rank=c["rank"], base=c["base"]))
        return CrewList(items=items, truncated=truncated)

    async def get_roster(
        self, p: Principal, crew_id: str, from_date: Any = None, to_date: Any = None
    ) -> Roster:
        async with self._client(p, ["roster.read"]) as client:
            raw = await client.get_roster(crew_id)
        roster = Roster.model_validate(raw)
        roster.days = [
            d
            for d in roster.days
            if (not from_date or d.date >= from_date) and (not to_date or d.date <= to_date)
        ]
        return roster

    async def find_cover(
        self, p: Principal, date: Any, base: str, rank: str | None = None, max_candidates: int = 5
    ) -> CoverOptions:
        found: list[CoverCandidate] = []
        checked = 0
        async with self._client(p, ["roster.read"]) as client:
            async for c in client.iter_crew(page_size=50):
                if c["base"] != base or (rank and c["rank"] != rank):
                    continue
                if checked == MAX_ROSTERS_PER_COVER_SEARCH:
                    break
                checked += 1
                days = (await client.get_roster(c["crew_id"]))["days"]
                duty = next((d["duty"] for d in days if d["date"] == date.isoformat()), None)
                if duty in _COVER_ORDER:
                    found.append(
                        CoverCandidate(
                            crew_id=c["crew_id"], name=c["name"], rank=c["rank"], duty_that_day=duty
                        )
                    )
                if sum(1 for f in found if f.duty_that_day == "STBY") >= max_candidates:
                    break
        found.sort(key=lambda f: _COVER_ORDER[f.duty_that_day])
        return CoverOptions(date=date, base=base, candidates=found[:max_candidates], checked=checked)

    async def propose_swap(
        self, p: Principal, requester_id: str, partner_id: str, date: Any, reason: str = ""
    ) -> SwapResult:
        """Runs only after a planner approved it (see ToolGuard.decide).

        Asks for swap.write only: the approval sign-in grants exactly the tool's scope, and the
        exchanger never adds scopes the person didn't grant.
        """
        async with self._client(p, ["swap.write"]) as client:
            raw = await client.request_swap(requester_id, partner_id, date.isoformat())
        return SwapResult.model_validate(raw)

    def describe(self, tool: str) -> str:
        return describe(DESCRIPTIONS, tool, self.ctx.description_variant)

    def tools(self) -> list[ToolSpec]:
        def summarize_swap(a: Mapping[str, Any]) -> str:
            why = f" Reason: {a['reason']}" if a.get("reason") else ""
            return f"Give {a['requester_id']}'s duty on {a['date']} to {a['partner_id']}.{why}"

        specs = [
            ToolSpec(
                "crew.list_crew", self.describe("crew.list_crew"), "roster.read", self.list_crew,
                title="List crew", input_model=ListCrewInput, output_model=CrewList, idempotent=True,
            ),
            ToolSpec(
                "crew.get_roster", self.describe("crew.get_roster"), "roster.read", self.get_roster,
                title="Read a roster", input_model=GetRosterInput, output_model=Roster, idempotent=True,
            ),
            ToolSpec(
                "crew.find_cover", self.describe("crew.find_cover"), "roster.read", self.find_cover,
                title="Find cover", input_model=FindCoverInput, output_model=CoverOptions, idempotent=True,
            ),
            ToolSpec(
                "crew.propose_swap", self.describe("crew.propose_swap"), "swap.write", self.propose_swap,
                title="Propose a duty swap", input_model=ProposeSwapInput, output_model=SwapResult,
                writes=True, summarize=summarize_swap,
            ),
        ]  # fmt: skip
        check_table(DESCRIPTIONS, [s.name for s in specs])
        return specs


def build(ctx: PluginContext | None = None) -> CrewPlugin:
    return CrewPlugin(ctx or PluginContext())
