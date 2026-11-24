"""Chat model client for any OpenAI-compatible chat-completions API. Groq by default.

Why not an SDK: this is ~200 lines over httpx, so every retry and limit is visible and tested.
Groq's free plan allows 30 requests a minute, 1,000 a day, 8,000 tokens a minute and 200,000 a
day per model. The client paces itself under the per-minute request limit, waits out short 429s
(the token-per-minute limit sends those), and stops with LLMQuotaExceeded when the wait is too
long (a daily limit), so an eval run ends cleanly with partial results instead of hanging.
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

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message, retry_after)  # both in args, so copies of the error keep the wait
        self.message, self.retry_after = message, retry_after

    def __str__(self) -> str:
        return self.message


class LLMToolCallFailed(LLMError):
    """The model tried to call a tool but wrote an invalid call (Groq: 400 tool_use_failed).
    That is the model's mistake, not an outage: evals score it as a wrong answer."""


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
    cached_tokens: int = 0  # prompt tokens served from the provider's cache


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
    details = usage.get("prompt_tokens_details") or {}
    return Completion(
        content=choice.get("content"),
        tool_calls=tuple(calls),
        usage=Usage(
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            int(details.get("cached_tokens") or 0),
        ),
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
        min_interval: float = 2.2,
        max_wait: float = 65.0,
        retries: int = 5,
        temperature: float = 0.0,
        seed: int | None = 7,
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise LLMError("no API key for the model provider (set AGENT_LLM_API_KEY)")
        self.name, self.url = model, base_url.rstrip("/") + "/chat/completions"
        self.min_interval, self.max_wait, self.retries = min_interval, max_wait, retries
        self.temperature, self.seed, self.reasoning_effort = temperature, seed, reasoning_effort
        self._sleep, self._clock, self._last = sleep, clock, -1e9
        self._lock = asyncio.Lock()
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": "application/json",
            # Only gzip: httpx also offers zstd and brotli when those packages happen to be
            # installed, which makes the response encoding depend on the environment.
            "accept-encoding": "gzip",
        }
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, headers=headers)

    async def probe(self) -> dict[str, Any]:
        """One tiny request shaped like a real one (a tool, seed, tool_choice). For diagnosing a
        provider before spending a day's quota. The prompt is fixed, so the reply is safe to print."""
        tool = {
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Get the current time in a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "user", "content": "What time is it in Gothenburg? Use the tool."}],
            "temperature": self.temperature,
            "tools": [tool],
            "tool_choice": "auto",
        }
        if self.seed is not None:
            body["seed"] = self.seed
        if self.reasoning_effort and takes_reasoning_effort(self.name):
            body["reasoning_effort"] = self.reasoning_effort
        resp = await self._http.post(self.url, json=body)
        out: dict[str, Any] = {"url": self.url, "model": self.name, "response": describe(resp)}
        try:
            data = resp.json()
        except ValueError:
            out.update(ok=False, problem="body is not JSON", body_start=resp.text[:300])
            return out
        if resp.status_code != 200:
            out.update(ok=False, problem=_error_code(resp))
            return out
        try:
            c = parse_completion(data)
        except LLMError as exc:
            out.update(ok=False, problem=str(exc), keys=sorted(data) if isinstance(data, dict) else None)
            return out
        out.update(ok=True, tool_calls=[(t.name, t.arguments) for t in c.tool_calls], content=c.content)
        return out

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
        if self.reasoning_effort and takes_reasoning_effort(self.name):
            body["reasoning_effort"] = self.reasoning_effort
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
                try:
                    data = resp.json()
                except ValueError:
                    # A 200 with an empty or non-JSON body: say what arrived, never crash the run.
                    log.warning("llm_bad_body", extra={"model": self.name, "response": describe(resp)})
                    if attempt < self.retries:
                        await self._sleep(2.0 * (attempt + 1))
                        continue
                    raise LLMError(
                        f"model provider sent a body that is not JSON ({describe(resp)})"
                    ) from None
                completion = parse_completion(data, latency)
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
                    raise LLMQuotaExceeded(f"rate limited by the model provider, retry in {wait:.0f}s", wait)
                await self._sleep(wait)
                continue
            if resp.status_code == 400 and _error_code(resp) == "tool_use_failed":
                raise LLMToolCallFailed("the model wrote an invalid tool call")
            if resp.status_code >= 500 and attempt < self.retries:
                await self._sleep(2.0 * (attempt + 1))
                continue
            raise LLMError(f"model provider returned {resp.status_code}: {_error_code(resp)}")
        raise LLMError("model call failed")  # pragma: no cover  (the loop always returns or raises)


def takes_reasoning_effort(model: str) -> bool:
    """gpt-oss and o-series models take low/medium/high; others reject the values or ignore them."""
    name = model.rsplit("/", 1)[-1]
    return name.startswith(("gpt-oss", "o1", "o3", "o4"))


class FallbackChat:
    """Tries models in order. A model whose quota is used up is skipped until its provider said it
    could be retried, so a public console keeps answering on the next model instead of failing.
    Each answer records which model gave it (Completion.model). Evals don't use this: they
    measure one model."""

    def __init__(
        self, models: Sequence[OpenAICompatibleChat], clock: Callable[[], float] = time.monotonic
    ) -> None:
        if not models:
            raise LLMError("no models configured")
        self.models, self.clock = list(models), clock
        self.name = self.models[0].name
        self._resting: dict[int, float] = {}  # model's position -> when it may be tried again

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        waits = []
        for i, model in enumerate(self.models):
            until = self._resting.get(i, 0.0)
            if until > self.clock():
                waits.append(until - self.clock())
                continue
            try:
                c = await model.complete(messages, tools)
            except LLMQuotaExceeded as exc:
                self._resting[i] = self.clock() + max(exc.retry_after, 60.0)
                waits.append(max(exc.retry_after, 60.0))
                log.warning("llm_fallback", extra={"model": model.name, "retry_in": round(exc.retry_after)})
                continue
            return c if c.model else Completion(c.content, c.tool_calls, c.usage, c.latency_ms, model.name)
        wait = min(waits) if waits else 60.0
        raise LLMQuotaExceeded(f"every model is out of quota, retry in {wait:.0f}s", wait)

    async def probe(self) -> dict[str, Any]:
        return await self.models[0].probe()

    async def aclose(self) -> None:
        for m in self.models:
            await m.aclose()


def describe(resp: httpx.Response) -> str:
    """What a response was, without its content: safe to log."""
    ct = resp.headers.get("content-type") or "none"
    enc = resp.headers.get("content-encoding") or "none"
    return f"HTTP {resp.status_code}, content-type {ct}, content-encoding {enc}, {len(resp.content)} bytes"


def _error_code(resp: httpx.Response) -> str:
    """Only the provider's error code, never the request echo (it may contain the prompt)."""
    try:
        err = resp.json().get("error") or {}
        return str(err.get("code") or err.get("type") or "error")[:60] if isinstance(err, dict) else "error"
    except ValueError:
        return "error"
