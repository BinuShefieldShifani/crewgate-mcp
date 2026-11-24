"""The eval harness itself: statistics, scoring, stopping on budget, and the suites end to end
with stand-in models, so a real run spends quota only on the model's answers."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from crewgate.agent.llm import (
    Completion,
    LLMError,
    LLMQuotaExceeded,
    LLMToolCallFailed,
    Message,
    ToolCall,
    ToolDef,
    Usage,
)
from evals import run as runner
from evals.common import Budget, BudgetExhausted, load_jsonl, percentile, wilson
from evals.offline import eval_guard, render_offline, run_offline
from evals.scenarios import render_scenarios, run_scenarios
from evals.selection import DATASET, args_match, expand, mcnemar_p, render_selection, run_selection
from tests.helpers.rule_model import RuleModel


def test_wilson_interval() -> None:
    lo, hi = wilson(33, 36)
    assert 0.78 < lo < 0.79 and 0.97 < hi < 0.98
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(36, 36)[1] == 1.0


def test_percentile_nearest_rank() -> None:
    assert percentile([5, 1, 3, 2, 4], 50) == 3
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([], 95) == 0.0


def test_budget() -> None:
    b = Budget(2)
    b.take()
    b.take()
    assert b.left == 0
    with pytest.raises(BudgetExhausted):
        b.take()


def test_load_jsonl_skips_comments_and_names_bad_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.jsonl"
    f.write_text('// comment\n{"a": 1}\n\n{bad\n')
    with pytest.raises(ValueError, match=r"x\.jsonl:4"):
        load_jsonl(f)
    f.write_text('// comment\n{"a": 1}\n')
    assert load_jsonl(f) == [{"a": 1}]


def test_mcnemar() -> None:
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(6, 0) == pytest.approx(0.03125)
    assert mcnemar_p(3, 3) == 1.0


def test_args_match_is_case_insensitive_for_codes() -> None:
    assert args_match({"base": "GOT", "adults": 2}, {"base": "got", "adults": 2, "x": 1})
    assert not args_match({"adults": 2}, {"adults": 1})
    assert not args_match({"base": "GOT"}, {})


def test_dataset_is_well_formed() -> None:
    rows = load_jsonl(DATASET)
    assert len(rows) >= 30 and len({r["id"] for r in rows}) == len(rows)
    tools = {"none", "crew_list_crew", "crew_get_roster", "crew_find_cover", "crew_propose_swap",
             "booking_search_flights", "booking_get_offer", "booking_get_seat_map", "booking_book_flight",
             "booking_get_booking", "gateway_approval_status"}  # fmt: skip
    for r in rows:
        assert set(r["expect"]) <= tools, r["id"]
        msgs = expand(r.get("context", []))
        for i, m in enumerate(msgs):  # every tool call is answered right after
            if m.get("tool_calls"):
                assert (
                    msgs[i + 1]["role"] == "tool" and msgs[i + 1]["tool_call_id"] == m["tool_calls"][0]["id"]
                )
    with pytest.raises(ValueError):
        expand([{"weird": 1}])


class Keyword:
    """Picks a tool from keywords. Only here to drive the selection harness."""

    name = "keyword"

    def __init__(self, fail_after: int | None = None, error_on: str | None = None) -> None:
        self.calls, self.fail_after, self.error_on = 0, fail_after, error_on

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise LLMQuotaExceeded("daily limit")
        text = messages[-1]["content"].lower()
        if self.error_on and self.error_on in text:
            raise LLMError("bad request")
        names = {t["function"]["name"] for t in tools}
        assert len(names) == 10
        pick = (
            "booking_search_flights" if "flight" in text else "crew_list_crew" if "captain" in text else None
        )
        if pick is None:
            return Completion("Could you tell me more?", (), Usage(1000, 5), 50.0)
        return Completion(
            None,
            (ToolCall("c", pick, {"base": "GOT", "rank": "Captain", "origin": "ARN"}),),
            Usage(1000, 5),
            80.0,
        )


async def test_selection_scores_every_variant_and_compares_them() -> None:
    r = await run_selection(Keyword(), Budget(500))
    assert r["complete"] and r["model_calls"] == 3 * r["cases"]
    v = {x["variant"]: x for x in r["variants"]}
    assert set(v) == {"minimal", "guided", "contract"}
    assert (
        v["minimal"]["tool_definitions_chars"]
        < v["guided"]["tool_definitions_chars"]
        < v["contract"]["tool_definitions_chars"]
    )
    g = v["guided"]
    assert 0 < g["accuracy"] < 1 and g["accuracy_ci95"][0] < g["accuracy"] < g["accuracy_ci95"][1]
    assert g["by_category"]["no_tool"]["accuracy"] == 1.0  # it never calls a tool on those
    assert any(c["id"] == "crew-list-base" and c["tool_ok"] and c["args_ok"] for c in g["cases"])
    assert len(r["comparisons"]) == 3 and r["comparisons"][0]["p_value"] == 1.0
    md = render_selection(r)
    assert "| guided |" in md and "McNemar" in md and "Best:" in md


async def test_selection_stops_cleanly_on_quota_and_keeps_errors() -> None:
    r = await run_selection(
        Keyword(fail_after=5, error_on="python"), Budget(500), variants=["guided", "contract"]
    )
    assert not r["complete"] and len(r["variants"]) == 1
    g = r["variants"][0]
    assert g["scored"] == 5 and g["stopped"] == "daily limit" and "(partial: 5/36)" in render_selection(r)
    r = await run_selection(Keyword(error_on="python"), Budget(500), variants=["guided"])
    assert r["variants"][0]["errors"] == 1


async def test_selection_respects_the_budget() -> None:
    r = await run_selection(Keyword(), Budget(10), variants=["minimal", "guided"])
    assert r["model_calls"] == 10 and not r["complete"]


async def test_scenarios_pass_with_a_rule_following_model() -> None:
    """If this passes, a failed scenario with a real model is the model's doing, not the check's."""
    r = await run_scenarios(RuleModel(), Budget(200))
    failed = [(s["id"], [c for c in s["checks"] if not c["ok"]]) for s in r["scenarios"] if not s["passed"]]
    assert not failed
    assert r["complete"] and r["passed"] == r["total"] == 10
    assert r["false_confirmed"] == 0 and r["writes_without_approval"] == 0
    assert r["caught_by_guard"] >= 3  # the rule model over-claims after proposing; the guard catches it
    assert "10/10 scenarios passed" in render_scenarios(r)


async def test_scenarios_stop_on_budget() -> None:
    r = await run_scenarios(RuleModel(), Budget(5))
    assert not r["complete"] and "budget" in (r["stopped"] or "")
    assert "partial" in render_scenarios(r)


class OverClaimer(RuleModel):
    """Says 'booked' at the end of every turn, so the guard is tested against a stubborn model."""

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        c = await super().complete(messages, tools)
        return c if c.tool_calls else Completion("Your flight is booked and confirmed!", (), c.usage, 1.0)


async def test_an_over_claiming_model_never_reaches_the_user() -> None:
    r = await run_scenarios(OverClaimer(), Budget(200), only=["pending-202", "order-failed", "price-change"])
    assert r["false_confirmed"] == 0
    assert r["drafts_over_claiming"] >= 3 and r["caught_by_guard"] >= 3


def test_guard_eval_numbers() -> None:
    g = eval_guard()
    assert g["unsafe_passed"] == 0 and g["true_claims_blocked"] == 0 and g["honest_blocked"] == 0
    assert g["claim_checks"] == g["claim_drafts"] * 8


async def test_offline_suite_passes() -> None:
    r = await run_offline(overhead_calls=10)
    assert r["passed"], {k: v for k, v in r["checks"].items() if not v}
    assert r["security"]["blocked"] == r["security"]["total"] >= 20
    assert r["quirks"]["handled"] == r["quirks"]["total"] >= 10
    assert "attacks refused" in render_offline(r)


async def test_runner_writes_results_and_the_job_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert await runner.main(["offline", "--overhead-calls", "5", "--out", str(tmp_path)]) == 0
    data: dict[str, Any] = json.loads((tmp_path / "offline.json").read_text())
    assert data["passed"] and "Offline evals" in summary.read_text()


async def test_runner_model_suites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Settings:
        max_steps = 8

        def __init__(self, **_: Any) -> None:
            pass

        def chat_model(self) -> Any:
            m = RuleModel()
            m.aclose = _noop  # type: ignore[attr-defined]
            return m

    async def _noop() -> None:
        return None

    monkeypatch.setattr(runner, "AgentSettings", Settings)
    code = await runner.main(["scenarios", "--only", "no-baggage,injection", "--out", str(tmp_path)])
    assert code == 0 and json.loads((tmp_path / "scenarios.json").read_text())["passed"] == 2
    code = await runner.main(["selection", "--variants", "minimal", "--budget", "3", "--out", str(tmp_path)])
    assert code == 0 and not json.loads((tmp_path / "selection.json").read_text())["complete"]
    with pytest.raises(SystemExit):
        await runner.main(["scenarios", "--only", "nope", "--out", str(tmp_path)])


async def test_selection_stops_after_three_model_errors_in_a_row() -> None:
    class Broken(Keyword):
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
            self.calls += 1
            raise LLMError("model provider sent a body that is not JSON")

    model = Broken()
    r = await run_selection(model, Budget(500))
    assert model.calls == 3 and not r["complete"]
    assert "3 model errors in a row" in (r["variants"][0]["stopped"] or "")


async def test_runner_probe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class Probe:
        async def probe(self) -> dict[str, Any]:
            return {"ok": False, "problem": "body is not JSON"}

        async def aclose(self) -> None:
            return None

    class Settings:
        def __init__(self, **_: Any) -> None:
            pass

        def chat_model(self) -> Any:
            return Probe()

    monkeypatch.setattr(runner, "AgentSettings", Settings)
    assert await runner.main(["probe"]) == 1
    assert "body is not JSON" in capsys.readouterr().out


async def test_an_invalid_tool_call_is_scored_wrong() -> None:
    class Fumbles(Keyword):
        async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
            if "captain" in messages[-1]["content"].lower():
                raise LLMToolCallFailed("bad call")
            return await super().complete(messages, tools)

    r = await run_selection(Fumbles(), Budget(500), variants=["guided"])
    case = next(c for c in r["variants"][0]["cases"] if c["id"] == "crew-list-base")
    assert case["got"] == "invalid_tool_call" and case["tool_ok"] is False and r["complete"]


class Obedient(RuleModel):
    """Does what the text hidden in the airline's name says: books straight after searching."""

    async def complete(self, messages: Sequence[Message], tools: Sequence[ToolDef]) -> Completion:
        last = messages[-1]
        if (
            last.get("role") == "tool"
            and "SYSTEM NOTICE" in last["content"]
            and '"offers"' in last["content"]
        ):
            offer = json.loads(last["content"])["offers"][0]
            args = {
                "offer_id": offer["offer_id"],
                "expected_total": offer["total_amount"],
                "currency": offer["currency"],
                "market": "SE",
                "passengers": [{"title": "ms", "given_name": "Anna", "family_name": "Berg",
                                "born_on": "1990-04-02", "gender": "f", "email": "anna@example.com",
                                "phone_number": "+46701234567"}],
            }  # fmt: skip
            return Completion(None, (ToolCall("inj", "booking_book_flight", args),), Usage(500, 20))
        return await super().complete(messages, tools)


async def test_an_injected_booking_is_stopped_before_the_gateway() -> None:
    r = await run_scenarios(Obedient(), Budget(50), only=["injection"])
    s = r["scenarios"][0]
    assert s["passed"] and s["writes_blocked"] == 1 and r["writes_without_approval"] == 0
    assert "stopped by the agent: 1" in render_scenarios(r)
