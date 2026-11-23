"""The agent's link to the crewgate gateway, through the official MCP client (Streamable HTTP).

The agent holds a user's access token and nothing else: no partner credentials, no approval
rights. Everything it can do goes through the gateway's checks.
"""

import contextlib
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .llm import ToolDef


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    is_error: bool
    data: dict[str, Any]  # structured content, or the parsed error body
    text: str  # what the model is shown (trimmed)
    latency_ms: float


def compact_schema(schema: Any) -> Any:
    """Drop the "title" annotations pydantic adds to every schema node. They repeat the field
    names, cost prompt tokens on every call and tell the model nothing. Property names are
    kept, including a property that is itself called "title"."""
    if isinstance(schema, list):
        return [compact_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: compact_schema(sub) for name, sub in value.items()}
        else:
            out[key] = compact_schema(value)
    return out


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace {"$ref": "#/$defs/X"} with X itself and drop $defs. Some providers behind
    OpenAI-compatible APIs reject references in tool parameters; our schemas have no cycles."""
    defs = schema.get("$defs") or {}

    def walk(node: Any, depth: int = 0) -> Any:
        if depth > 20:
            raise ValueError("schema nests too deep (a reference cycle?)")
        if isinstance(node, list):
            return [walk(x, depth + 1) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/") and ref[8:] in defs:
            merged = {**defs[ref[8:]], **{k: v for k, v in node.items() if k != "$ref"}}
            return walk(merged, depth + 1)
        return {k: walk(v, depth + 1) for k, v in node.items() if k != "$defs"}

    result: dict[str, Any] = walk(schema)
    return result


def openai_tool(tool: types.Tool) -> ToolDef:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": compact_schema(
                inline_refs(tool.input_schema or {"type": "object", "properties": {}})
            ),
        },
    }


def outcome_of(name: str, result: types.CallToolResult, latency_ms: float, max_chars: int) -> ToolOutcome:
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    data: dict[str, Any] = dict(result.structured_content or {})
    if not data and text:
        with contextlib.suppress(ValueError):
            parsed = json.loads(text)
            data = parsed if isinstance(parsed, dict) else {"result": parsed}
    if data:  # compact JSON: the same facts in fewer prompt tokens
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    shown = text if len(text) <= max_chars else text[:max_chars] + " ...[truncated]"
    return ToolOutcome(name, bool(result.is_error), data, shown, latency_ms)


class GatewayTools:
    def __init__(self, client: Client, tools: list[types.Tool], max_chars: int = 3500) -> None:
        self.client, self.tools, self.max_chars = client, {t.name: t for t in tools}, max_chars

    @property
    def instructions(self) -> str:
        return self.client.instructions or ""

    def definitions(self) -> list[ToolDef]:
        return [openai_tool(t) for t in self.tools.values()]

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name not in self.tools:  # the model made a tool up: say so, don't send it
            body = {"error": "unknown_tool", "message": f"there is no tool named {name}"}
            return ToolOutcome(name, True, body, json.dumps(body), 0.0)
        started = time.perf_counter()
        result = await self.client.call_tool(name, dict(arguments))
        return outcome_of(name, result, (time.perf_counter() - started) * 1000, self.max_chars)

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(
        cls, url: str, token: str, *, asgi_app: Any = None, max_chars: int = 3500
    ) -> AsyncIterator["GatewayTools"]:
        """Open an MCP session. `asgi_app` runs the gateway in-process (tests and evals)."""
        transport = httpx2.ASGITransport(app=asgi_app) if asgi_app is not None else None
        http = httpx2.AsyncClient(
            headers={"authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(30.0, read=120.0),
            transport=transport,
        )
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            listed = await client.list_tools()
            yield cls(client, list(listed.tools), max_chars)
