"""tools/list and tools/call over raw JSON-RPC: schemas, structured output and every error shape."""

import json
from typing import Any

import jsonschema
import pytest

from .conftest import Env

TOOLS = {
    "gateway_approval_status",
    "crew_list_crew",
    "crew_get_roster",
    "crew_find_cover",
    "crew_propose_swap",
}


async def tools(env: Env) -> dict[str, dict[str, Any]]:
    r = await env.rpc("tools/list")
    return {t["name"]: t for t in r.json()["result"]["tools"]}


def error_of(result: dict[str, Any]) -> dict[str, Any]:
    assert result["result"]["isError"] is True
    assert "structuredContent" not in result["result"] or result["result"]["structuredContent"] is None
    body: dict[str, Any] = json.loads(result["result"]["content"][0]["text"])
    return body


async def test_tool_list_and_annotations(env: Env) -> None:
    listed = await tools(env)
    assert set(listed) == TOOLS
    swap = listed["crew_propose_swap"]
    assert swap["annotations"]["destructiveHint"] is True and swap["annotations"]["readOnlyHint"] is False
    assert "awaiting_approval" in json.dumps(swap["outputSchema"])
    assert "does NOT make the swap" in swap["description"]
    read = listed["crew_get_roster"]["annotations"]
    assert read["readOnlyHint"] is True and read["idempotentHint"] is True
    for t in listed.values():
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"].get("additionalProperties") is False  # strict inputs everywhere
        jsonschema.Draft202012Validator.check_schema(t["outputSchema"])


async def test_structured_output_matches_the_published_schema(env: Env) -> None:
    schemas = await tools(env)
    calls = {
        "crew_get_roster": {"crew_id": "C0001", "from_date": "2026-12-02", "to_date": "2026-12-04"},
        "crew_list_crew": {"base": "GOT", "limit": 3},
        "crew_find_cover": {"date": "2026-12-03", "base": "GOT", "max_candidates": 3},
    }
    for tool, args in calls.items():
        result = (await env.call(tool, args))["result"]
        assert result["isError"] is False
        jsonschema.validate(result["structuredContent"], schemas[tool]["outputSchema"])
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    roster = (await env.call("crew_get_roster", calls["crew_get_roster"]))["result"]["structuredContent"]
    assert [d["date"] for d in roster["days"]] == ["2026-12-02", "2026-12-03", "2026-12-04"]
    crew = (await env.call("crew_list_crew", calls["crew_list_crew"]))["result"]["structuredContent"]
    assert len(crew["items"]) == 3 and crew["truncated"] and {c["base"] for c in crew["items"]} == {"GOT"}
    assert "email" not in json.dumps(crew)  # data minimisation
    cover = (await env.call("crew_find_cover", calls["crew_find_cover"]))["result"]["structuredContent"]
    duties = [c["duty_that_day"] for c in cover["candidates"]]
    assert duties == sorted(duties, key=lambda d: d != "STBY") and set(duties) <= {"STBY", "OFF"}


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"crew_id": "42"}, "crew_id"),
        ({"crew_id": "C0001", "admin": True}, "admin"),
        ({"crew_id": "C0001", "from_date": "2026-12-05", "to_date": "2026-12-01"}, ""),
        ({}, "crew_id"),
    ],
)
async def test_bad_arguments_are_tool_errors_the_model_can_fix(
    env: Env, args: dict[str, Any], field: str
) -> None:
    err = error_of(await env.call("crew_get_roster", args))
    assert err["error"] == "invalid_arguments"
    assert any(d["field"] == field for d in err["details"])


async def test_unknown_tool_is_a_protocol_error(env: Env) -> None:
    r = await env.rpc("tools/call", {"name": "crew_delete_everything", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    assert "Unknown tool" in r.json()["error"]["message"]


async def test_entitlements_apply_per_calling_app(env: Env) -> None:
    dashboard = env.agent_token(azp="roster-dashboard")
    assert (await env.call("crew_get_roster", {"crew_id": "C0001"}, token=dashboard))["result"][
        "isError"
    ] is False
    err = error_of(await env.call("crew_find_cover", {"date": "2026-12-03", "base": "GOT"}, token=dashboard))
    assert err["error"] == "tool_not_entitled"
    unknown = env.agent_token(azp="some-other-app")
    assert (
        error_of(await env.call("crew_get_roster", {"crew_id": "C0001"}, token=unknown))["error"]
        == "unknown_client"
    )


async def test_scope_is_checked_before_anything_is_proposed(env: Env) -> None:
    reader = env.agent_token(scope="openid roster.read")
    args = {"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-03"}
    assert error_of(await env.call("crew_propose_swap", args, token=reader))["error"] == "insufficient_scope"
    assert env.gw.guard.approvals._items == {}


async def test_rate_limit_reaches_the_model_with_retry_after(env: Env) -> None:
    token = env.agent_token("ratelimited")
    for _ in range(30):
        await env.call("gateway_approval_status", {"approval_id": "x" * 20}, token=token)
    err = error_of(await env.call("gateway_approval_status", {"approval_id": "x" * 20}, token=token))
    assert err["error"] == "rate_limited:user_minute" and err["retry_after"] >= 1


async def test_partner_errors_are_mapped_to_safe_messages(env: Env) -> None:
    err = error_of(await env.call("crew_get_roster", {"crew_id": "C9999"}))
    assert err == {
        "error": "partner_not_found",
        "message": "not found at the partner",
        "detail": "crew_not_found",
    }


async def test_crashes_never_leak_details(env: Env) -> None:
    spec = env.gw.specs["crew.get_roster"]

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("db password is hunter2")

    object.__setattr__(spec, "handler", broken)
    result = await env.call("crew_get_roster", {"crew_id": "C0001"})
    assert error_of(result) == {"error": "internal_error", "message": "the gateway hit an unexpected error"}
    assert "hunter2" not in json.dumps(result)


async def test_approval_status_of_someone_elses_request_looks_missing(env: Env) -> None:
    args = {"requester_id": "C0001", "partner_id": "C0002", "date": "2026-12-03"}
    pending = (await env.call("crew_propose_swap", args))["result"]["structuredContent"]
    erik = env.agent_token("erik")
    err = error_of(
        await env.call("gateway_approval_status", {"approval_id": pending["approval_id"]}, token=erik)
    )
    assert err["error"] == "approval_not_found"
