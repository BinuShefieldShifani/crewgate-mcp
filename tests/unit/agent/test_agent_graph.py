"""The LangGraph agent against the whole gateway in-process, through the real MCP client.

The model is scripted, so these tests pin down the graph: tools run through the gateway,
facts are recorded, every reply passes the guard, limits hold.
"""

import datetime as dt
import json
from typing import Any

import pytest

from crewgate.agent import Agent, GatewayTools
from crewgate.agent.graph import OUT_OF_STEPS
from crewgate.agent.llm import Completion, ToolCall
from crewgate.agent.tools import compact_schema, inline_refs
from tests.helpers.scripted_llm import ScriptedChat, call, find, last_tool, say
from tests.helpers.stack import MCP_URL, gateway_stack

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
SEARCH = {"origin": "ARN", "destination": "LHR", "departure_date": DAY, "market": "SE"}


def book_args(messages: Any) -> dict[str, Any]:
    offer = last_tool(messages)
    return {
        "offer_id": offer["offer_id"],
        "expected_total": offer["total_amount"],
        "currency": offer["currency"],
        "market": "SE",
        "passengers": [PERSON],
    }


def cheapest(messages: Any) -> dict[str, Any]:
    o = last_tool(messages)["offers"][0]
    return {"offer_id": o["offer_id"], "quoted_total": o["total_amount"]}


async def test_booking_flow_through_the_gateway() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app) as tools,
    ):
        model = ScriptedChat(
            [
                call("booking_search_flights", SEARCH),
                call("booking_get_offer", cheapest),
                call("booking_book_flight", book_args),
                say("Done! Your flight is booked."),
            ]
        )
        agent = Agent(model, tools, today=dt.date(2026, 11, 23))
        turn = await agent.ask("Book me the cheapest ARN to LHR")
        assert turn.tool_calls == ["booking_search_flights", "booking_get_offer", "booking_book_flight"]
        assert not turn.guard.ok and turn.draft == "Done! Your flight is booked."
        assert "waiting for approval" in turn.reply
        assert agent.history[-1] == {"role": "assistant", "content": turn.reply}  # what the user saw
        assert st.duffel.posts("/air/orders") == []
        assert "2026-11-23" in model.seen[0][0]["content"]  # today's date is in the system prompt
        assert "crewgate gives you" in model.seen[0][0]["content"]  # and the gateway's instructions
        assert turn.steps == 4 and turn.tokens == 4 * 110

        approval_id = next(iter(agent.facts.approvals))
        assert (await st.decide(approval_id)).status_code == 200
        model.steps = [
            call("gateway_approval_status", {"approval_id": approval_id}),
            call("booking_get_booking", lambda m: {"booking_id": find(r'"booking_id":"(bk_[^"]+)"', m)}),
            say("Your flight is confirmed."),
        ]
        turn = await agent.ask("Is it booked?")
        assert turn.guard.ok and turn.reply == "Your flight is confirmed."
        assert any(b.confirmed for b in agent.facts.booking_models())


async def test_tool_errors_reach_the_model_as_data() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(scope="openid booking.read"), asgi_app=st.app) as tools,
    ):
        model = ScriptedChat([call("booking_book_flight", {"offer_id": "off_x"}), say("That did not work.")])
        turn = await Agent(model, tools).ask("book it")
        result = json.loads(model.seen[1][-1]["content"])
        assert result["error"] == "invalid_arguments"
        assert turn.trace[1]["error"] == "invalid_arguments"


async def test_made_up_tools_and_bad_json_are_answered_not_sent() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app) as tools,
    ):
        bad = Completion(None, (ToolCall("c1", "booking_search_flights", {}, "{oops", True),))
        model = ScriptedChat([call("booking_cancel_everything", {}), lambda _: bad, say("Sorry.")])
        turn = await Agent(model, tools).ask("hi")
        assert json.loads(model.seen[1][-1]["content"])["error"] == "unknown_tool"
        assert json.loads(model.seen[2][-1]["content"])["error"] == "invalid_arguments"
        assert turn.reply == "Sorry." and st.duffel.calls == []


async def test_step_limit_ends_the_turn_safely() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app) as tools,
    ):
        model = ScriptedChat([call("booking_search_flights", SEARCH)] * 3)
        agent = Agent(model, tools, max_steps=2)
        turn = await agent.ask("search forever")
        assert turn.reply == OUT_OF_STEPS and turn.steps == 2
        # the unanswered tool call is dropped, so the next request to the model stays valid
        assert agent.history[-1] == {"role": "assistant", "content": OUT_OF_STEPS}
        assert agent.history[-2]["role"] == "tool"


async def test_long_tool_results_are_trimmed() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app, max_chars=200) as tools,
    ):
        model = ScriptedChat([call("booking_search_flights", SEARCH), say("ok")])
        await Agent(model, tools).ask("search")
        shown = model.seen[1][-1]["content"]
        assert len(shown) < 260 and shown.endswith("...[truncated]")


async def test_tool_definitions_are_compact_and_complete() -> None:
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app) as tools,
    ):
        defs = {d["function"]["name"]: d["function"] for d in tools.definitions()}
        assert len(defs) == 10
        book = defs["booking_book_flight"]["parameters"]
        assert "title" not in book and "title" not in book["properties"]["offer_id"]
        assert "$defs" not in json.dumps(book) and "$ref" not in json.dumps(book)
        passenger = book["properties"]["passengers"]["items"]["properties"]
        assert "title" in passenger and "title" not in passenger["given_name"]


def test_inline_refs() -> None:
    person = {"type": "object", "properties": {"n": {"type": "string"}}}
    schema = {
        "$defs": {"P": person},
        "properties": {"p": {"$ref": "#/$defs/P", "description": "who"}, "q": {"$ref": "#/elsewhere"}},
    }
    assert inline_refs(schema) == {
        "properties": {
            "p": {"type": "object", "properties": {"n": {"type": "string"}}, "description": "who"},
            "q": {"$ref": "#/elsewhere"},
        }
    }
    loop = {"$defs": {"A": {"items": {"$ref": "#/$defs/A"}}}, "items": {"$ref": "#/$defs/A"}}
    with pytest.raises(ValueError, match="too deep"):
        inline_refs(loop)


def test_compact_schema_keeps_property_names() -> None:
    schema = {
        "title": "X",
        "properties": {"title": {"title": "Title", "type": "string"}},
        "anyOf": [{"title": "A"}],
    }
    assert compact_schema(schema) == {"properties": {"title": {"type": "string"}}, "anyOf": [{}]}


async def test_scopes_are_enforced_on_calls_not_hidden_from_the_list() -> None:
    """The model sees every tool; the gateway decides each call."""
    async with (
        gateway_stack() as st,
        GatewayTools.connect(MCP_URL, st.agent_token(scope="openid roster.read"), asgi_app=st.app) as tools,
    ):
        assert len(tools.tools) == 10
        out = await tools.call("booking_search_flights", SEARCH)
        assert out.is_error and out.data["error"] == "insufficient_scope"
