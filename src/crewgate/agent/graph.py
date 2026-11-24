"""The agent as a LangGraph state graph.

    START -> think -> (tool calls?) -> act -> think -> ... -> language -> guard -> END
                   \\-> (no tool calls) -> language -> guard
                   \\-> (out of steps) ------------> guard

think  one model call with the gateway's tools
act    runs the tool calls through the MCP gateway, records facts from the results
language  one rewrite if the reply is clearly in another language than the user's message
guard  checks the draft reply against those facts (reply_guard.py). Every reply passes here:
       there is no edge to END that skips it.

The state carries the facts (booking states, approval requests) between turns, so a later
"is it booked?" is checked against what the tools said, not against what the model remembers.
"""

import datetime as dt
import json
import operator
import time
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from .language import mismatch, rewrite_request
from .llm import ChatModel, Completion, LLMError, LLMToolCallFailed, Message
from .prompts import system_prompt
from .reply_guard import Facts, GuardDecision, facts_from_tool, guard_reply
from .tools import GatewayTools

TOOL_CALL_FAILED = "Sorry, I made a mistake while working on that. Could you say it again, maybe more simply?"
OUT_OF_STEPS = (
    "I could not finish this within the allowed number of steps. Nothing new was booked or changed."
)


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


class AgentState(TypedDict, total=False):
    messages: Annotated[list[Message], operator.add]
    bookings: Annotated[dict[str, dict[str, Any]], _merge]
    approvals: Annotated[dict[str, dict[str, Any]], _merge]
    booking_tools_used: Annotated[bool, operator.or_]
    steps: Annotated[int, operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    out_of_steps: bool
    draft: str  # set when the reply was rewritten into the user's language
    final: str
    guard: dict[str, Any]


@dataclass
class TurnResult:
    reply: str
    draft: str
    guard: GuardDecision
    tool_calls: list[str]
    trace: list[dict[str, Any]]
    steps: int

    @property
    def tokens(self) -> int:
        return sum(t.get("prompt_tokens", 0) + t.get("completion_tokens", 0) for t in self.trace)


@dataclass
class Agent:
    model: ChatModel
    tools: GatewayTools
    max_steps: int = 8
    market: str = "SE"
    today: dt.date | None = None
    history: list[Message] = field(default_factory=list)
    facts: Facts = field(default_factory=lambda: Facts({}, {}))

    def __post_init__(self) -> None:
        self.system = system_prompt(self.tools.instructions, self.market, self.today)
        self.tool_defs = self.tools.definitions()
        self.graph = self._build()

    # ---- nodes ------------------------------------------------------------------------------

    async def _think(self, state: AgentState) -> AgentState:
        messages: list[Message] = [{"role": "system", "content": self.system}, *state.get("messages", [])]
        try:
            c = await self.model.complete(messages, self.tool_defs)
        except LLMToolCallFailed:
            c = Completion(content=TOOL_CALL_FAILED)  # the turn ends through the guard as usual
        step = {
            "kind": "llm",
            "ms": round(c.latency_ms, 1),
            "prompt_tokens": c.usage.prompt_tokens,
            "completion_tokens": c.usage.completion_tokens,
            "tool_calls": [t.name for t in c.tool_calls],
        }
        return {"messages": [c.as_message()], "steps": 1, "trace": [step]}

    async def _act(self, state: AgentState) -> AgentState:
        out: list[Message] = []
        trace: list[dict[str, Any]] = []
        bookings: dict[str, Any] = {}
        approvals: dict[str, Any] = {}
        used = False
        for call in state["messages"][-1].get("tool_calls") or []:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                text = json.dumps(
                    {"error": "invalid_arguments", "message": "arguments were not a JSON object"}
                )
                out.append({"role": "tool", "tool_call_id": call["id"], "content": text})
                trace.append({"kind": "tool", "name": name, "error": "bad_json", "ms": 0.0})
                continue
            started = time.perf_counter()
            outcome = await self.tools.call(name, args)
            used = used or name.startswith("booking_") or name == "gateway_approval_status"
            b, a = facts_from_tool(name, outcome.is_error, outcome.data)
            bookings.update(b)
            approvals.update(a)
            out.append({"role": "tool", "tool_call_id": call["id"], "content": outcome.text})
            trace.append(
                {
                    "kind": "tool",
                    "name": name,
                    "arguments": args,
                    "error": outcome.data.get("error") if outcome.is_error else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
        return {
            "messages": out,
            "trace": trace,
            "bookings": bookings,
            "approvals": approvals,
            "booking_tools_used": used,
        }

    async def _guard(self, state: AgentState) -> AgentState:
        last = state["messages"][-1]
        draft = (
            OUT_OF_STEPS
            if state.get("out_of_steps")
            else state.get("draft") or str(last.get("content") or "")
        )
        facts = Facts(
            state.get("bookings", {}), state.get("approvals", {}), state.get("booking_tools_used", False)
        )
        decision = guard_reply(draft, facts)
        return {
            "final": decision.reply,
            "guard": asdict(decision),
            "trace": [{"kind": "guard", "ok": decision.ok}],
        }

    async def _language(self, state: AgentState) -> AgentState:
        """One rewrite when the reply is clearly not in the language of the user's latest message."""
        reply = str(state["messages"][-1].get("content") or "")
        user = next((m["content"] for m in reversed(state["messages"]) if m.get("role") == "user"), "")
        want = mismatch(str(user), reply)
        if want is None:
            return {}
        request: list[Message] = [
            {"role": "system", "content": "You rewrite text into another language and change nothing else."},
            {"role": "user", "content": rewrite_request(reply, want)},
        ]
        try:
            c = await self.model.complete(request, [])
        except LLMError:
            return {"trace": [{"kind": "rewrite", "to": want, "ok": False}]}  # keep the original
        step = {
            "kind": "llm",
            "ms": round(c.latency_ms, 1),
            "prompt_tokens": c.usage.prompt_tokens,
            "completion_tokens": c.usage.completion_tokens,
            "tool_calls": [],
        }
        if not c.content or c.tool_calls:
            return {"trace": [step, {"kind": "rewrite", "to": want, "ok": False}]}
        return {"draft": c.content, "steps": 1, "trace": [step, {"kind": "rewrite", "to": want, "ok": True}]}

    async def _out_of_steps(self, state: AgentState) -> AgentState:
        return {"out_of_steps": True}

    def _route(self, state: AgentState) -> Literal["act", "language", "out_of_steps"]:
        if not state["messages"][-1].get("tool_calls"):
            return "language"
        return "act" if state.get("steps", 0) < self.max_steps else "out_of_steps"

    def _build(self) -> Any:
        g = StateGraph(AgentState)
        g.add_node("think", self._think)
        g.add_node("act", self._act)
        g.add_node("out_of_steps", self._out_of_steps)
        g.add_node("language", self._language)
        g.add_node("guard", self._guard)
        g.add_edge(START, "think")
        g.add_conditional_edges("think", self._route)
        g.add_edge("act", "think")
        g.add_edge("language", "guard")
        g.add_edge("out_of_steps", "guard")
        g.add_edge("guard", END)
        return g.compile()

    # ---- one user turn ----------------------------------------------------------------------

    async def ask(self, text: str) -> TurnResult:
        state: AgentState = {
            "messages": [*self.history, {"role": "user", "content": text}],
            "bookings": dict(self.facts.bookings),
            "approvals": dict(self.facts.approvals),
            "booking_tools_used": self.facts.booking_tools_used,
            "steps": 0,
            "trace": [],
            "out_of_steps": False,
            "draft": "",
        }
        final: AgentState = await self.graph.ainvoke(state, {"recursion_limit": 4 * self.max_steps + 8})
        decision = GuardDecision(**{**final["guard"], "claims": tuple(final["guard"]["claims"])})
        messages = list(final["messages"])
        # History keeps what the user saw. A last message with unanswered tool calls (out of steps)
        # is dropped: chat APIs reject a tool call with no tool result after it.
        if messages and messages[-1].get("role") == "assistant":
            messages.pop()
        messages.append({"role": "assistant", "content": decision.reply})
        self.history = messages
        self.facts = Facts(
            final.get("bookings", {}), final.get("approvals", {}), final.get("booking_tools_used", False)
        )
        trace = final.get("trace", [])
        return TurnResult(
            reply=decision.reply,
            draft=decision.draft,
            guard=decision,
            tool_calls=[t["name"] for t in trace if t["kind"] == "tool"],
            trace=trace,
            steps=final.get("steps", 0),
        )
