"""A chat model that plays a script, for agent tests that need no real model.

Each step is a function of the messages so far, so a step can use ids the tools returned
(an offer_id, an approval_id) the way a real model would.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from crewgate.agent.llm import Completion, Message, ToolCall, ToolDef, Usage

Step = Callable[[Sequence[Message]], Completion]


def say(text: str) -> Step:
    return lambda _: Completion(content=text, usage=Usage(100, 10))


def call(name: str, args: dict[str, Any] | Callable[[Sequence[Message]], dict[str, Any]]) -> Step:
    def step(messages: Sequence[Message]) -> Completion:
        a = args(messages) if callable(args) else args
        n = sum(1 for m in messages if m.get("role") == "assistant")
        return Completion(
            content=None, tool_calls=(ToolCall(f"call_{n}", name, a, json.dumps(a)),), usage=Usage(100, 10)
        )

    return step


def last_tool(messages: Sequence[Message]) -> dict[str, Any]:
    """The last tool result, parsed."""
    for m in reversed(messages):
        if m.get("role") == "tool":
            data: dict[str, Any] = json.loads(m["content"])
            return data
    raise AssertionError("no tool result yet")


def find(pattern: str, messages: Sequence[Message]) -> str:
    """First match of a regex in any tool result, newest first (e.g. an approval id)."""
    for m in reversed(messages):
        if m.get("role") == "tool" and (hit := re.search(pattern, m["content"])):
            return hit.group(1) if hit.groups() else hit.group(0)
    raise AssertionError(f"{pattern} not found in tool results")


@dataclass
class ScriptedChat:
    steps: list[Step]
    name: str = "scripted"
    seen: list[list[Message]] = field(default_factory=list)
    tools_seen: list[list[ToolDef]] = field(default_factory=list)

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        self.seen.append(list(messages))
        self.tools_seen.append(list(tools))
        if not self.steps:
            raise AssertionError("the script ran out of steps")
        return self.steps.pop(0)(messages)
