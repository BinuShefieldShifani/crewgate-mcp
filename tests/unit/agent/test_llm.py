"""The model client: request shape, parsing, pacing, and every retry and stop rule."""

import json
from typing import Any

import httpx
import pytest

from crewgate.agent.llm import (
    LLMError,
    LLMQuotaExceeded,
    LLMToolCallFailed,
    OpenAICompatibleChat,
    parse_completion,
    retry_after_seconds,
)
from crewgate.agent.settings import AgentSettings

OK = {
    "model": "gpt-4o-mini",
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "crew_list_crew", "arguments": '{"base":"GOT"}'},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 900, "completion_tokens": 12},
}


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def chat(
    responses: list[Any], clock: Clock | None = None, **kw: Any
) -> tuple[OpenAICompatibleChat, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    clock = clock or Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    model = OpenAICompatibleChat(
        base_url="https://models.test/inference/",
        model="openai/gpt-oss-120b",
        api_key="dev-only-key",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
        **kw,
    )
    return model, seen


async def test_request_shape_and_parsing() -> None:
    model, seen = chat([httpx.Response(200, json=OK)])
    tools = [{"type": "function", "function": {"name": "crew_list_crew", "parameters": {"type": "object"}}}]
    c = await model.complete([{"role": "user", "content": "hi"}], tools)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://models.test/inference/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer dev-only-key"
    assert body["temperature"] == 0 and body["seed"] == 7 and body["tool_choice"] == "auto"
    assert body["tools"] == tools
    assert c.tool_calls[0].name == "crew_list_crew" and c.tool_calls[0].arguments == {"base": "GOT"}
    assert c.usage.prompt_tokens == 900 and c.model == "gpt-4o-mini"
    assert c.as_message()["tool_calls"][0]["function"]["arguments"] == '{"base":"GOT"}'
    await model.aclose()


async def test_no_tools_means_no_tool_choice() -> None:
    model, seen = chat(
        [httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})], seed=None
    )
    c = await model.complete([{"role": "user", "content": "hi"}], [])
    body = json.loads(seen[0].content)
    assert (
        "tools" not in body
        and "seed" not in body
        and c.content == "hello"
        and c.as_message() == {"role": "assistant", "content": "hello"}
    )


def test_bad_tool_arguments_are_flagged_not_raised() -> None:
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "x", "arguments": "{not json"}},
                        {"function": {"name": "y", "arguments": "[1]"}},
                    ]
                }
            }
        ]
    }
    c = parse_completion(body)
    assert [t.bad_json for t in c.tool_calls] == [True, True] and c.tool_calls[0].arguments == {}
    assert c.tool_calls[0].id == "call_0"


def test_no_choices_is_an_error() -> None:
    with pytest.raises(LLMError):
        parse_completion({"choices": []})


async def test_paces_calls_under_the_per_minute_limit() -> None:
    clock = Clock()
    model, _ = chat([httpx.Response(200, json=OK), httpx.Response(200, json=OK)], clock, min_interval=4.2)
    await model.complete([], [])
    await model.complete([], [])
    assert clock.slept == [pytest.approx(4.2)]


async def test_short_429_waits_and_retries() -> None:
    clock = Clock()
    model, seen = chat(
        [httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json=OK)],
        clock,
        min_interval=0,
    )
    await model.complete([], [])
    assert len(seen) == 2 and 3.0 in clock.slept


async def test_long_429_stops_with_quota_exceeded() -> None:
    model, seen = chat([httpx.Response(429, headers={"retry-after": "40000"})], min_interval=0)
    with pytest.raises(LLMQuotaExceeded):
        await model.complete([], [])
    assert len(seen) == 1


async def test_429_every_time_gives_up() -> None:
    model, seen = chat([httpx.Response(429, headers={"retry-after": "1"})] * 3, min_interval=0, retries=2)
    with pytest.raises(LLMQuotaExceeded):
        await model.complete([], [])
    assert len(seen) == 3


async def test_server_errors_and_network_errors_are_retried() -> None:
    model, seen = chat(
        [httpx.Response(503), httpx.ConnectError("down"), httpx.Response(200, json=OK)], min_interval=0
    )
    await model.complete([], [])
    assert len(seen) == 3


async def test_network_down_for_good() -> None:
    model, _ = chat([httpx.ConnectError("down")] * 2, min_interval=0, retries=1)
    with pytest.raises(LLMError, match="unreachable"):
        await model.complete([], [])


async def test_client_error_reports_only_the_code() -> None:
    echo = {
        "error": {"code": "tokens_limit_reached", "message": "Request body too large: SECRET PROMPT TEXT"}
    }
    model, _ = chat([httpx.Response(413, json=echo)], min_interval=0)
    with pytest.raises(LLMError) as exc:
        await model.complete([], [])
    assert "tokens_limit_reached" in str(exc.value) and "SECRET" not in str(exc.value)


async def test_non_json_error_body() -> None:
    model, _ = chat([httpx.Response(400, text="<html>")], min_interval=0)
    with pytest.raises(LLMError, match="400: error"):
        await model.complete([], [])


def test_retry_after_forms() -> None:
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "12"})) == 12
    assert retry_after_seconds(httpx.Response(429, headers={"x-ratelimit-timeremaining": "30s"})) == 30
    assert (
        retry_after_seconds(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        == 0
    )
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "soon"}), default=5) == 5
    assert retry_after_seconds(httpx.Response(429)) == 10


def test_needs_a_key() -> None:
    with pytest.raises(LLMError, match="no API key"):
        OpenAICompatibleChat(base_url="https://x", model="m", api_key="")


def test_settings_read_only_their_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "dev-only-gh")  # must never be sent to a model provider
    s = AgentSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.api_key() == ""
    with pytest.raises(LLMError):
        s.chat_model()
    monkeypatch.setenv("AGENT_LLM_API_KEY", "dev-only-explicit")
    s = AgentSettings(_env_file=None)  # type: ignore[call-arg]
    model = s.chat_model()
    assert s.api_key() == "dev-only-explicit" and model.name == "openai/gpt-oss-120b"
    assert model.url == "https://api.groq.com/openai/v1/chat/completions" and model.reasoning_effort == "low"


async def test_headers_pin_the_encoding() -> None:
    model, seen = chat([httpx.Response(200, json=OK)])
    await model.complete([], [])
    h = seen[0].headers
    assert h["accept-encoding"] == "gzip" and h["accept"] == "application/json"
    assert "x-github-api-version" not in h


async def test_a_200_that_is_not_json_is_retried_then_reported() -> None:
    model, seen = chat([httpx.Response(200, content=b""), httpx.Response(200, json=OK)], min_interval=0)
    assert (await model.complete([], [])).tool_calls
    assert len(seen) == 2
    model, _ = chat(
        [httpx.Response(200, content=b"", headers={"content-type": "text/plain"})] * 2,
        min_interval=0,
        retries=1,
    )
    with pytest.raises(
        LLMError, match=r"not JSON \(HTTP 200, content-type text/plain, content-encoding none, 0 bytes\)"
    ):
        await model.complete([], [])


@pytest.mark.parametrize(
    ("response", "ok", "problem"),
    [
        (httpx.Response(200, json=OK), True, None),
        (httpx.Response(200, text="<html>hi</html>"), False, "body is not JSON"),
        (httpx.Response(401, json={"error": {"code": "unauthorized"}}), False, "unauthorized"),
        (httpx.Response(200, json={"weird": 1}), False, "the model returned no choices"),
    ],
)
async def test_probe(response: httpx.Response, ok: bool, problem: str | None) -> None:
    model, seen = chat([response])
    out = await model.probe()
    body = json.loads(seen[0].content)
    assert body["tools"][0]["function"]["name"] == "get_time" and body["seed"] == 7
    assert out["ok"] is ok and out.get("problem") == problem
    if ok:
        assert out["tool_calls"] == [("crew_list_crew", {"base": "GOT"})]
    if problem == "body is not JSON":
        assert out["body_start"] == "<html>hi</html>"


async def test_reasoning_effort_and_cached_tokens() -> None:
    usage = {
        **OK,
        "usage": {
            "prompt_tokens": 900,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 800},
        },
    }
    model, seen = chat([httpx.Response(200, json=usage)], reasoning_effort="low")
    c = await model.complete([], [])
    assert json.loads(seen[0].content)["reasoning_effort"] == "low"
    assert c.usage.cached_tokens == 800


async def test_an_invalid_tool_call_is_the_models_mistake_not_an_outage() -> None:
    body = {
        "error": {"code": "tool_use_failed", "message": "Failed to call a function", "failed_generation": "x"}
    }
    model, seen = chat([httpx.Response(400, json=body)], min_interval=0)
    with pytest.raises(LLMToolCallFailed):
        await model.complete([], [])
    assert len(seen) == 1  # not retried


def test_reasoning_effort_only_for_models_that_take_it() -> None:
    from crewgate.agent.llm import takes_reasoning_effort

    assert takes_reasoning_effort("openai/gpt-oss-120b") and takes_reasoning_effort("o3-mini")
    assert not takes_reasoning_effort("qwen/qwen3.8-27b") and not takes_reasoning_effort("llama-3.3-70b")


async def test_reasoning_effort_is_not_sent_to_other_models() -> None:
    model, seen = chat([httpx.Response(200, json=OK)], reasoning_effort="low")
    model.name = "qwen/qwen3.8-27b"
    await model.complete([], [])
    assert "reasoning_effort" not in json.loads(seen[0].content)


async def test_fallback_moves_on_when_a_models_quota_is_used_up() -> None:
    from crewgate.agent.llm import FallbackChat

    clock = Clock()
    first, seen_first = chat([httpx.Response(429, headers={"retry-after": "3600"})], clock, min_interval=0)
    second, seen_second = chat(
        [httpx.Response(200, json=OK), httpx.Response(200, json=OK)], clock, min_interval=0
    )
    second.name = "openai/gpt-oss-20b"
    fb = FallbackChat([first, second], clock=clock)
    assert fb.name == "openai/gpt-oss-120b" or fb.name == first.name
    c = await fb.complete([], [])
    assert c.tool_calls and len(seen_first) == 1 and len(seen_second) == 1
    await fb.complete([], [])  # the first model rests: not even tried
    assert len(seen_first) == 1 and len(seen_second) == 2


async def test_fallback_gives_up_with_the_shortest_wait() -> None:
    from crewgate.agent.llm import FallbackChat

    clock = Clock()
    a, _ = chat([httpx.Response(429, headers={"retry-after": "900"})], clock, min_interval=0)
    b, _ = chat([httpx.Response(429, headers={"retry-after": "300"})], clock, min_interval=0)
    fb = FallbackChat([a, b], clock=clock)
    with pytest.raises(LLMQuotaExceeded) as exc:
        await fb.complete([], [])
    assert exc.value.retry_after == 300
    with pytest.raises(LLMQuotaExceeded) as exc:  # both resting now: no request at all
        await fb.complete([], [])
    assert 0 < exc.value.retry_after <= 300
    with pytest.raises(LLMError):
        FallbackChat([])


async def test_settings_build_the_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_LLM_API_KEY", "dev-only-explicit")
    fb = AgentSettings(_env_file=None).chat_model_with_fallbacks()  # type: ignore[call-arg]
    assert [m.name for m in fb.models] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    await fb.aclose()
