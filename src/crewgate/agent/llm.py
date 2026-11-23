"""Chat model client for any OpenAI-compatible chat-completions API. GitHub Models by default.

Why not an SDK: this is ~150 lines over httpx, so every retry and limit is visible and tested.
GitHub Models' free tier allows about 15 requests a minute and 150 a day per low-tier model, and
8000 input tokens per request. The client paces itself under the per-minute limit, waits out
short 429s, and stops with LLMQuotaExceeded when the wait is too long (the daily limit), so an
eval run ends cleanly with partial results instead of hanging.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

log = logging.getLogger("crewgate.agent.llm")

Message = dict[str, Any]  # OpenAI chat format: {"role": ..., "content": ..., "tool_calls": ...}
ToolDef = dict[str, Any]  # {"type": "function", "function": {"name", "description", "parameters"}}


class LLMError(Exception):
    pass


class LLMQuotaExceeded(LLMError):
    """The provider wants us to wait longer than we are willing to (usually the daily limit)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    bad_json: bool = False


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    model: str = ""

    def as_message(self) -> Message:
        msg: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.raw_arguments or json.dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
        return msg


class ChatModel(Protocol):
    name: str

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion: ...


def parse_completion(body: dict[str, Any], latency_ms: float = 0.0) -> Completion:
    try:
        choice = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("the model returned no choices") from exc
    calls: list[ToolCall] = []
    for raw in choice.get("tool_calls") or []:
        fn = raw.get("function") or {}
        text = fn.get("arguments") or "{}"
        try:
            args = json.loads(text)
            bad = not isinstance(args, dict)
        except json.JSONDecodeError:
            args, bad = {}, True
        calls.append(
            ToolCall(
                str(raw.get("id") or f"call_{len(calls)}"),
                str(fn.get("name") or ""),
                args if isinstance(args, dict) else {},
                text,
                bad,
            )
        )
    usage = body.get("usage") or {}
    return Completion(
        content=choice.get("content"),
        tool_calls=tuple(calls),
        usage=Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
        latency_ms=latency_ms,
        model=str(body.get("model") or ""),
    )


def retry_after_seconds(resp: httpx.Response, default: float = 10.0) -> float:
    for header in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-timeremaining"):
        value = resp.headers.get(header)
        if not value:
            continue
        try:
            return max(0.0, float(value.rstrip("s")))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                continue
    return default


class OpenAICompatibleChat:
    """POST {base_url}/chat/completions with tools. Temperature 0 and a fixed seed for repeatability."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        timeout: float = 60.0,
        min_interval: float = 4.2,
        max_wait: float = 65.0,
        retries: int = 3,
        temperature: float = 0.0,
        seed: int | None = 7,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise LLMError("no API key for the model provider (set AGENT_LLM_API_KEY or GITHUB_TOKEN)")
        self.name, self.url = model, base_url.rstrip("/") + "/chat/completions"
        self.min_interval, self.max_wait, self.retries = min_interval, max_wait, retries
        self.temperature, self.seed = temperature, seed
        self._sleep, self._clock, self._last = sleep, clock, -1e9
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pace(self) -> None:
        async with self._lock:
            wait = self._last + self.min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last = self._clock()

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": list(messages),
            "temperature": self.temperature,
        }
        if self.seed is not None:
            body["seed"] = self.seed
        if tools:
            body["tools"], body["tool_choice"] = list(tools), "auto"
        for attempt in range(self.retries + 1):
            await self._pace()
            started = time.perf_counter()
            try:
                resp = await self._http.post(self.url, json=body)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise LLMError(f"model provider unreachable: {type(exc).__name__}") from exc
                await self._sleep(2.0 * (attempt + 1))
                continue
            latency = (time.perf_counter() - started) * 1000
            if resp.status_code == 200:
                completion = parse_completion(resp.json(), latency)
                log.info(
                    "llm_call",
                    extra={
                        "model": self.name,
                        "ms": round(latency),
                        "prompt_tokens": completion.usage.prompt_tokens,
                        "completion_tokens": completion.usage.completion_tokens,
                        "tool_calls": [c.name for c in completion.tool_calls],
                    },
                )
                return completion
            if resp.status_code == 429:
                wait = retry_after_seconds(resp)
                if wait > self.max_wait or attempt == self.retries:
                    raise LLMQuotaExceeded(f"rate limited by the model provider, retry in {wait:.0f}s")
                await self._sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < self.retries:
                await self._sleep(2.0 * (attempt + 1))
                continue
            raise LLMError(f"model provider returned {resp.status_code}: {_error_code(resp)}")
        raise LLMError("model call failed")  # pragma: no cover  (the loop always returns or raises)


def _error_code(resp: httpx.Response) -> str:
    """Only the provider's error code, never the request echo (it may contain the prompt)."""
    try:
        err = resp.json().get("error") or {}
        return str(err.get("code") or err.get("type") or "error")[:60] if isinstance(err, dict) else "error"
    except ValueError:
        return "error"
