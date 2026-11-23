"""Tool-description variants: complete for every tool, served by the gateway, and naming
other tools only by names the model can actually see."""

import re

import pytest

from crewgate.core.descriptions import VARIANTS, DescriptionError, check_table, describe
from crewgate.plugins.booking import DESCRIPTIONS as BOOKING
from crewgate.plugins.crew import DESCRIPTIONS as CREW
from crewgate.plugins.gateway import DESCRIPTIONS as GATEWAY
from tests.helpers.stack import gateway_stack

TABLES = [BOOKING, CREW, GATEWAY]
ALL_TOOLS = {t for table in TABLES for t in table["guided"]}
WIRE_NAMES = {t.replace(".", "_") for t in ALL_TOOLS}


def test_unknown_variant_and_missing_text() -> None:
    with pytest.raises(DescriptionError, match="unknown description variant"):
        describe(BOOKING, "booking.get_offer", "verbose")
    with pytest.raises(DescriptionError, match="no 'minimal' description"):
        describe({"minimal": {}}, "booking.get_offer", "minimal")


def test_check_table_catches_gaps_and_strays() -> None:
    table = {v: {"a.x": "text"} for v in VARIANTS}
    check_table(table, ["a.x"])
    with pytest.raises(DescriptionError, match="missing"):
        check_table(table, ["a.x", "a.y"])
    with pytest.raises(DescriptionError, match="unknown tools"):
        check_table(table, [])


@pytest.mark.parametrize("variant", VARIANTS)
def test_descriptions_name_tools_by_their_mcp_names(variant: str) -> None:
    """booking.get_offer is invisible to a model; booking_get_offer is what it sees."""
    for table in TABLES:
        for tool, text in table[variant].items():
            dotted = re.findall(r"\b(?:booking|crew|gateway)\.[a-z_]+\b", text)
            assert not dotted, f"{variant} {tool} uses dotted names {dotted}"
            for ref in re.findall(
                r"\b(?:booking|crew|gateway)_(?:search|get|book|list|find|propose|approval)[a-z_]*\b", text
            ):
                assert ref in WIRE_NAMES, f"{variant} {tool} mentions unknown tool {ref}"


def test_variants_differ_in_the_way_they_claim_to() -> None:
    for tool in ALL_TOOLS:
        table = next(t for t in TABLES if tool in t["guided"])
        minimal, guided, contract = (table[v][tool] for v in VARIANTS)
        assert len(minimal) < 40 and "\n" not in minimal
        assert len(guided) > len(minimal)
        assert "Use when:" in contract or "Needs:" in contract


@pytest.mark.parametrize("variant", VARIANTS)
async def test_gateway_serves_the_configured_variant(variant: str) -> None:
    async with gateway_stack(variant) as st:
        r = await st.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "authorization": f"Bearer {st.agent_token()}",
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            },
        )
        tools = {t["name"]: t["description"] for t in r.json()["result"]["tools"]}
        assert tools["booking_get_offer"] == BOOKING[variant]["booking.get_offer"]
        assert tools["crew_find_cover"] == CREW[variant]["crew.find_cover"]
        assert tools["gateway_approval_status"] == GATEWAY[variant]["gateway.approval_status"]
