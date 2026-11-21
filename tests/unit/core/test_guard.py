"""The guard: refusals never reach the plugin, and everything lands in audit and metrics."""

from typing import Any

import pytest
from pydantic import SecretStr

from crewgate.core.audit import AuditLog, MemorySink, verify_chain
from crewgate.core.auth import Principal
from crewgate.core.entitlements import EntitlementPolicy
from crewgate.core.errors import ErrorCategory, PartnerError
from crewgate.core.guard import GuardDenied, ToolGuard
from crewgate.core.metrics import GatewayMetrics
from crewgate.core.plugin import ToolSpec
from crewgate.core.ratelimit import RateLimiter

KEY = SecretStr("dev-only-audit-key")
POLICY = EntitlementPolicy.from_dict(
    {
        "clients": {
            "agent": {"tools": ["crew.*", "booking.*"], "markets": ["SE"], "per_user_per_minute": 2},
            "dashboard": {"tools": ["crew.get_roster"]},
        }
    }
)


def principal(client: str = "agent", scopes: str = "roster.read") -> Principal:
    return Principal(
        subject="user-anna",
        client_id=client,
        username="anna",
        scopes=frozenset(scopes.split()),
        roles={},
        expires_at=2e9,
        token=SecretStr("t"),
    )


class Env:
    def __init__(self) -> None:
        self.sink = MemorySink()
        self.metrics = GatewayMetrics()
        self.calls: list[dict[str, Any]] = []
        self.guard = ToolGuard(POLICY, RateLimiter(lambda: 1e9), AuditLog(KEY, self.sink), self.metrics)

    def tool(
        self,
        name: str = "crew.get_roster",
        scope: str = "roster.read",
        fail: Exception | None = None,
        **kw: Any,
    ):  # type: ignore[no-untyped-def]
        async def handler(p: Principal, **args: Any) -> dict[str, Any]:
            self.calls.append({"who": p.subject, **args})
            if fail:
                raise fail
            return {"ok": True}

        return ToolSpec(name=name, description="d", required_scope=scope, handler=handler, **kw)

    def audit(self) -> list[dict[str, Any]]:
        import json

        return [json.loads(line) for line in self.sink.lines]


@pytest.fixture
def env() -> Env:
    return Env()


async def test_allowed_call_runs_and_is_recorded(env: Env) -> None:
    assert await env.guard.run(principal(), env.tool(), {"crew_id": "C0001"}) == {"ok": True}
    assert env.calls == [{"who": "user-anna", "crew_id": "C0001"}]
    (rec,) = env.audit()
    assert rec["decision"] == "allowed" and rec["outcome"] == "ok" and rec["tool"] == "crew.get_roster"
    assert (
        env.metrics.value("crewgate_tool_calls_total", tool="crew.get_roster", client="agent", outcome="ok")
        == 1
    )
    assert env.metrics.value("crewgate_tool_duration_seconds_count", tool="crew.get_roster") == 1
    assert env.metrics.value("crewgate_tool_calls_in_flight") == 0


@pytest.mark.parametrize(
    ("p", "spec_kw", "market", "status", "reason"),
    [
        (principal(client="unknown"), {}, None, 403, "unknown_client"),
        (principal(client="dashboard"), {"name": "crew.propose_swap"}, None, 403, "tool_not_entitled"),
        (principal(), {"name": "booking.search", "market_bound": True}, "NO", 403, "market_not_entitled"),
        (principal(), {"name": "booking.search", "market_bound": True}, None, 403, "market_required"),
        (
            principal(scopes="roster.read"),
            {"name": "crew.propose_swap", "scope": "swap.write"},
            None,
            403,
            "insufficient_scope",
        ),
    ],
)
async def test_refusals_never_reach_the_plugin(
    env: Env, p: Principal, spec_kw: dict[str, Any], market: str | None, status: int, reason: str
) -> None:
    with pytest.raises(GuardDenied) as err:
        await env.guard.run(p, env.tool(**spec_kw), {}, market)
    assert (err.value.status, err.value.reason) == (status, reason)
    assert env.calls == []
    (rec,) = env.audit()
    assert rec["decision"] == "denied" and rec["reason"] == reason and rec["outcome"] == "-"
    assert (
        env.metrics.value(
            "crewgate_denials_total", tool=env.tool(**spec_kw).name, client=p.client_id, reason=reason
        )
        == 1
    )


async def test_rate_limit_refusal_carries_headers(env: Env) -> None:
    for _ in range(2):
        await env.guard.run(principal(), env.tool(), {})
    with pytest.raises(GuardDenied) as err:
        await env.guard.run(principal(), env.tool(), {})
    assert err.value.status == 429 and err.value.reason == "rate_limited:user_minute"
    assert err.value.headers["Retry-After"] == "30"
    assert env.metrics.value("crewgate_rate_limited_total", client="agent", limit="user_minute") == 1
    assert len(env.calls) == 2


async def test_partner_failure_is_recorded_and_re_raised(env: Env) -> None:
    spec = env.tool(fail=PartnerError(ErrorCategory.UPSTREAM_UNAVAILABLE, "down"))
    with pytest.raises(PartnerError):
        await env.guard.run(principal(), spec, {})
    (rec,) = env.audit()
    assert rec["decision"] == "allowed" and rec["outcome"] == "error:upstream_unavailable"
    outcome = "error:upstream_unavailable"
    assert (
        env.metrics.value("crewgate_tool_calls_total", tool=spec.name, client="agent", outcome=outcome) == 1
    )


async def test_unexpected_crash_is_recorded(env: Env) -> None:
    with pytest.raises(RuntimeError):
        await env.guard.run(principal(), env.tool(fail=RuntimeError("bug")), {})
    assert env.audit()[0]["outcome"] == "error:internal"
    assert env.metrics.value("crewgate_tool_calls_in_flight") == 0


async def test_whole_session_leaves_a_valid_chain(env: Env) -> None:
    await env.guard.run(principal(), env.tool(), {})
    with pytest.raises(GuardDenied):
        await env.guard.run(principal(client="unknown"), env.tool(), {})
    await env.guard.run(principal(), env.tool(), {})
    assert verify_chain(env.sink.lines, KEY).ok
    assert env.metrics.value("crewgate_audit_records_total") == 3
    assert b"crewgate_tool_calls_total" in env.metrics.exposition()
