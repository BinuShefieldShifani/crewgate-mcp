"""Core PartnerClient against scripted responses: 5xx backoff, network errors, odd payloads."""

import httpx
import pytest
from pydantic import SecretStr

from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.partner_http import NoAuth, PartnerClient, RetryPolicy, StaticBearer, _dig


def scripted(responses):
    """MockTransport that returns the given responses (or raises exceptions) in order."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


async def _noop_sleep(_s: float) -> None:
    return None


def client_for(responses, **kw):
    transport, calls = scripted(responses)
    http = httpx.AsyncClient(transport=transport, base_url="http://partner")
    return PartnerClient(http, sleep=_noop_sleep, **kw), calls


async def test_retries_5xx_then_succeeds():
    pc, calls = client_for([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})])
    assert await pc.request("GET", "/x") == {"ok": 1}
    assert len(calls) == 3 and [e.status for e in pc.retry_log] == [503, 502]


async def test_5xx_after_budget_is_upstream_unavailable():
    pc, _ = client_for([httpx.Response(500)], policy=RetryPolicy(max_retries=2))
    with pytest.raises(PartnerError) as e:
        await pc.request("GET", "/x")
    assert e.value.category is ErrorCategory.UPSTREAM_UNAVAILABLE and e.value.status == 500


async def test_network_error_retried_then_mapped():
    pc, calls = client_for([httpx.ConnectError("boom")], policy=RetryPolicy(max_retries=1))
    with pytest.raises(PartnerError) as e:
        await pc.request("GET", "/x")
    assert e.value.category is ErrorCategory.UPSTREAM_UNAVAILABLE and len(calls) == 2


async def test_retry_after_beyond_budget_fails_fast_instead_of_blocking():
    pc, calls = client_for(
        [httpx.Response(429, headers={"Retry-After": "28"}), httpx.Response(200, json={})],
        policy=RetryPolicy(max_retry_after_seconds=5),
    )
    with pytest.raises(PartnerError) as e:
        await pc.request("GET", "/x")
    assert e.value.category is ErrorCategory.RATE_LIMITED and len(calls) == 1 and pc.retry_log == []


async def test_retry_after_within_budget_is_honoured_and_bad_values_tolerated():
    pc, _ = client_for(
        [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(200, json={}),
        ],
        policy=RetryPolicy(max_retry_after_seconds=5),
    )
    await pc.request("GET", "/x")
    assert [e.waited for e in pc.retry_log] == [3.0, 1.0]


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, ErrorCategory.VALIDATION),
        (404, ErrorCategory.NOT_FOUND),
        (409, ErrorCategory.CONFLICT),
        (422, ErrorCategory.VALIDATION),
        (418, ErrorCategory.UNKNOWN),
    ],
)
async def test_status_mapping(status, category):
    pc, _ = client_for([httpx.Response(status, text="plain text error")])
    with pytest.raises(PartnerError) as e:
        await pc.request("GET", "/x")
    assert e.value.category is category and not e.value.retryable


async def test_401_without_refreshable_auth_is_not_retried():
    pc, calls = client_for([httpx.Response(401, json={"errors": [{"code": "unauthorized"}]})], auth=NoAuth())
    with pytest.raises(PartnerError) as e:
        await pc.request("GET", "/x")
    assert e.value.category is ErrorCategory.AUTH and len(calls) == 1


async def test_static_bearer_sent_and_empty_body_ok():
    pc, calls = client_for([httpx.Response(204)], auth=StaticBearer(SecretStr("duffel_test_abc")))
    assert await pc.request("DELETE", "/x") is None
    assert calls[0].headers["Authorization"] == "Bearer duffel_test_abc"


async def test_paginate_supports_nested_cursor_key():
    pages = [
        httpx.Response(200, json={"data": [{"id": 1}], "meta": {"after": "c2"}}),
        httpx.Response(200, json={"data": [{"id": 2}], "meta": {"after": None}}),
    ]
    pc, calls = client_for(pages)
    items = [
        i
        async for i in pc.paginate(
            "/offers", items_key="data", cursor_param="after", next_cursor_key="meta.after"
        )
    ]
    assert [i["id"] for i in items] == [1, 2]
    assert calls[1].url.params["after"] == "c2"


def test_dig_handles_non_dict():
    assert _dig({"a": 1}, "a.b") is None


# ---- metrics ------------------------------------------------------------------------------


class _Refreshable:
    refreshable = True

    def __init__(self) -> None:
        self.forced = 0

    async def headers(self, force_refresh: bool = False) -> dict[str, str]:
        self.forced += force_refresh
        return {"Authorization": "Bearer t"}


async def test_partner_metrics_count_every_attempt_and_retry_cause():
    from crewgate.core.metrics import GatewayMetrics

    m = GatewayMetrics()
    responses = [
        httpx.ConnectError("down"),
        httpx.Response(401),
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(503),
        httpx.Response(200, json={"ok": 1}),
    ]
    pc, calls = client_for(responses, auth=_Refreshable(), name="crew-roster", metrics=m)
    assert await pc.request("GET", "/x") == {"ok": 1}
    assert len(calls) == 5

    def requests(cls: str) -> float:
        return m.value("crewgate_partner_requests_total", partner="crew-roster", status_class=cls)

    def retries(cause: str) -> float:
        return m.value("crewgate_partner_retries_total", partner="crew-roster", cause=cause)

    assert (requests("network_error"), requests("4xx"), requests("5xx"), requests("2xx")) == (1, 2, 1, 1)
    assert (retries("network"), retries("401_refresh"), retries("429"), retries("5xx")) == (1, 1, 1, 1)
