"""Run the evals and write the numbers.

    uv run python -m evals.run offline                       # no model, every PR
    uv run python -m evals.run selection --budget 110        # needs AGENT_LLM_API_KEY (Groq)
    uv run python -m evals.run scenarios --variant contract
    uv run python -m evals.run all

Results go to evals/out/<suite>.json and <suite>.md, and to the GitHub job summary when there
is one. Exit code 1 means a safety check failed (an unsafe reply passed, an attack got through,
a false "confirmed", an order without approval). Accuracy below target is reported, not failed:
it is a number to improve, not a gate.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from crewgate.agent.settings import AgentSettings
from crewgate.core.descriptions import VARIANTS

from .common import Budget, write_json
from .load import render_load, run_load
from .offline import render_offline, run_offline
from .scenarios import SCENARIOS, render_scenarios, run_scenarios
from .selection import render_selection, run_selection

OUT = Path(__file__).parent / "out"


def publish(name: str, result: dict[str, Any], markdown: str, out: Path) -> None:
    write_json(out / f"{name}.json", result)
    (out / f"{name}.md").write_text(markdown)
    print(markdown)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(markdown + "\n")


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.ERROR)  # the attack evals trigger expected warnings
    p = argparse.ArgumentParser(prog="evals.run")
    p.add_argument("suite", choices=["offline", "load", "selection", "scenarios", "all", "probe"])
    p.add_argument("--model", help="e.g. openai/gpt-oss-120b (default: AGENT_LLM_MODEL)")
    p.add_argument("--variants", default=",".join(VARIANTS), help="for selection, comma separated")
    p.add_argument("--variant", default="guided", help="descriptions the scenarios run with")
    p.add_argument("--budget", type=int, default=140, help="max model calls in this run")
    p.add_argument("--only", default="", help="scenario ids, comma separated")
    p.add_argument("--overhead-calls", type=int, default=300)
    p.add_argument("--out", type=Path, default=OUT)
    a = p.parse_args(argv)

    failed = False
    if a.suite in ("offline", "all"):
        r = await run_offline(a.overhead_calls)
        publish("offline", r, render_offline(r), a.out)
        failed |= not r["passed"]
    if a.suite == "offline":
        return 1 if failed else 0
    if a.suite == "load":
        r = await run_load()
        publish("load", r, render_load(r), a.out)
        return 0 if r["passed"] else 1

    settings = AgentSettings(**({"llm_model": a.model} if a.model else {}))
    model = settings.chat_model()
    budget = Budget(a.budget)
    if a.suite == "probe":
        try:
            result = await model.probe()
        finally:
            await model.aclose()
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    try:
        if a.suite in ("scenarios", "all"):
            only = [x for x in a.only.split(",") if x]
            unknown = set(only) - {s.id for s in SCENARIOS}
            if unknown:
                p.error(f"unknown scenarios: {sorted(unknown)}")
            r = await run_scenarios(model, budget, a.variant, settings.max_steps, only)
            publish("scenarios", r, render_scenarios(r), a.out)
            failed |= r["false_confirmed"] > 0 or r["writes_without_approval"] > 0
        if a.suite in ("selection", "all"):
            variants = [v for v in a.variants.split(",") if v]
            r = await run_selection(model, budget, variants)
            publish("selection", r, render_selection(r), a.out)
    finally:
        await model.aclose()
    print(json.dumps({"model_calls": budget.used, "budget": budget.limit}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
