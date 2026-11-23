"""Tool-selection accuracy for each tool-description variant.

For every case in datasets/selection.jsonl, the model gets the agent's system prompt, the tools
exactly as the gateway serves them under one variant, any earlier turns, and the user's message.
One model call per case. We score its FIRST move:

  tool right   the first tool called is one the case accepts ("none" = answer or ask, no tool)
  args right   when the tool is right and the case lists argument values, all of them match

Nothing is executed. Only the descriptions change between variants: same prompt, same cases,
same model, temperature 0, fixed seed. Differences between two variants are tested with an exact
McNemar test on the cases where they disagree, so a "win" on 36 cases isn't over-read.
"""

import datetime as dt
import json
import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from crewgate.agent.llm import ChatModel, Completion, LLMError, LLMQuotaExceeded, LLMToolCallFailed, Message
from crewgate.agent.prompts import system_prompt
from crewgate.agent.tools import GatewayTools
from crewgate.core.descriptions import VARIANTS
from tests.helpers.stack import MCP_URL, gateway_stack

from .common import Budget, BudgetExhausted, load_jsonl, pct, percentile, wilson

DATASET = Path(__file__).parent / "datasets" / "selection.jsonl"


def expand(context: Sequence[dict[str, Any]]) -> list[Message]:
    """Compact dataset turns -> chat messages, with real tool_call ids and tool results."""
    out: list[Message] = []
    for n, turn in enumerate(context):
        if "user" in turn:
            out.append({"role": "user", "content": turn["user"]})
        elif "assistant" in turn:
            out.append({"role": "assistant", "content": turn["assistant"]})
        elif "call" in turn:
            cid = f"ctx_{n}"
            out.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": turn["call"], "arguments": json.dumps(turn["args"])},
                        }
                    ],
                }
            )
            out.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(turn["result"])})
        else:
            raise ValueError(f"unknown context turn: {turn}")
    return out


def args_match(expected: dict[str, Any], got: dict[str, Any]) -> bool:
    for key, want in expected.items():
        have = got.get(key)
        if isinstance(want, str) and isinstance(have, str):
            if want.strip().lower() != have.strip().lower():
                return False
        elif want != have:
            return False
    return True


def mcnemar_p(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value: b and c are the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


async def run_variant(
    model: ChatModel, variant: str, cases: list[dict[str, Any]], budget: Budget, today: dt.date
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    stopped = None
    async with (
        gateway_stack(variant) as st,
        GatewayTools.connect(MCP_URL, st.agent_token(), asgi_app=st.app) as tools,
    ):
        system: Message = {"role": "system", "content": system_prompt(tools.instructions, "SE", today)}
        defs = tools.definitions()
        tool_chars = len(json.dumps(defs))
        for case in cases:
            messages = [system, *expand(case.get("context", [])), {"role": "user", "content": case["prompt"]}]
            invalid = False
            try:
                budget.take()
                c = await model.complete(messages, defs)
            except LLMToolCallFailed:  # the model's mistake: scored as wrong, not as an outage
                c, invalid = Completion(content=None), True
            except (BudgetExhausted, LLMQuotaExceeded) as exc:
                stopped = str(exc)
                break
            except LLMError as exc:
                rows.append({"id": case["id"], "category": case["category"], "error": str(exc)})
                if [r for r in rows[-3:] if "error" in r] == rows[-3:] and len(rows) >= 3:
                    stopped = f"3 model errors in a row, last: {exc}"  # don't burn the day's quota
                    break
                continue
            first = c.tool_calls[0] if c.tool_calls else None
            got = first.name if first else ("invalid_tool_call" if invalid else "none")
            tool_ok = got in case["expect"]
            args_ok = None
            if tool_ok and first is not None and case.get("args"):
                args_ok = args_match(case["args"], first.arguments)
            rows.append(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "expected": case["expect"],
                    "got": got,
                    "arguments": first.arguments if first else None,
                    "tool_ok": tool_ok,
                    "args_ok": args_ok,
                    "prompt_tokens": c.usage.prompt_tokens,
                    "cached_tokens": c.usage.cached_tokens,
                    "completion_tokens": c.usage.completion_tokens,
                    "latency_ms": round(c.latency_ms, 1),
                    "reply": (c.content or "")[:200] if not first else None,
                }
            )
    return summarize(variant, rows, tool_chars, stopped, len(cases))


def summarize(
    variant: str, rows: list[dict[str, Any]], tool_chars: int, stopped: str | None, planned: int
) -> dict[str, Any]:
    scored = [r for r in rows if "tool_ok" in r]
    right = sum(r["tool_ok"] for r in scored)
    with_args = [r for r in scored if r["args_ok"] is not None]
    by_cat: dict[str, Any] = {}
    for cat in sorted({r["category"] for r in scored}):
        sub = [r for r in scored if r["category"] == cat]
        by_cat[cat] = {"n": len(sub), "accuracy": sum(r["tool_ok"] for r in sub) / len(sub)}
    none_cases = [r for r in scored if "none" in r["expected"] and len(r["expected"]) == 1]
    tool_cases = [r for r in scored if "none" not in r["expected"]]
    confusions = Counter(f"{'/'.join(r['expected'])} -> {r['got']}" for r in scored if not r["tool_ok"])
    return {
        "variant": variant,
        "planned": planned,
        "scored": len(scored),
        "errors": len(rows) - len(scored),
        "complete": stopped is None and len(scored) == planned,
        "stopped": stopped,
        "accuracy": right / len(scored) if scored else 0.0,
        "accuracy_ci95": wilson(right, len(scored)),
        "args_accuracy": sum(r["args_ok"] for r in with_args) / len(with_args) if with_args else None,
        "args_n": len(with_args),
        "false_tool_rate": sum(r["got"] != "none" for r in none_cases) / len(none_cases)
        if none_cases
        else None,
        "missed_tool_rate": sum(r["got"] == "none" for r in tool_cases) / len(tool_cases)
        if tool_cases
        else None,
        "by_category": by_cat,
        "confusions": confusions.most_common(),
        "tool_definitions_chars": tool_chars,
        "mean_prompt_tokens": round(sum(r["prompt_tokens"] for r in scored) / len(scored)) if scored else 0,
        "tokens_used": sum(r["prompt_tokens"] + r["completion_tokens"] for r in scored),
        "cached_share": (
            sum(r["cached_tokens"] for r in scored) / max(1, sum(r["prompt_tokens"] for r in scored))
        ),
        "latency_p50_ms": percentile([r["latency_ms"] for r in scored], 50),
        "latency_p95_ms": percentile([r["latency_ms"] for r in scored], 95),
        "cases": rows,
    }


def compare(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairwise exact McNemar on cases both variants scored."""
    out = []
    for i, a in enumerate(results):
        for b in results[i + 1 :]:
            ra = {r["id"]: r["tool_ok"] for r in a["cases"] if "tool_ok" in r}
            rb = {r["id"]: r["tool_ok"] for r in b["cases"] if "tool_ok" in r}
            shared = ra.keys() & rb.keys()
            only_a = sum(ra[k] and not rb[k] for k in shared)
            only_b = sum(rb[k] and not ra[k] for k in shared)
            out.append(
                {
                    "a": a["variant"],
                    "b": b["variant"],
                    "shared": len(shared),
                    "only_a_right": only_a,
                    "only_b_right": only_b,
                    "p_value": round(mcnemar_p(only_a, only_b), 4),
                }
            )
    return out


async def run_selection(
    model: ChatModel,
    budget: Budget,
    variants: Sequence[str] = VARIANTS,
    today: dt.date | None = None,
    dataset: Path = DATASET,
) -> dict[str, Any]:
    cases = load_jsonl(dataset)
    results = []
    for variant in variants:
        results.append(await run_variant(model, variant, cases, budget, today or dt.date.today()))
        if not results[-1]["complete"]:
            break
    best = max(results, key=lambda r: (r["accuracy"], -r["tool_definitions_chars"])) if results else None
    return {
        "suite": "selection",
        "model": model.name,
        "cases": len(cases),
        "variants": results,
        "best": best["variant"] if best else None,
        "comparisons": compare(results),
        "complete": all(r["complete"] for r in results) and len(results) == len(variants),
        "model_calls": budget.used,
    }


TARGET_ACCURACY = 0.90


def render_selection(r: dict[str, Any]) -> str:
    lines = [
        f"## Tool selection ({r['model']}, {r['cases']} cases, temperature 0)",
        "",
        "| Variant | Tool right | 95% CI | Args right | Called a tool when it shouldn't | Missed a tool | "
        "Tool list size | Tokens used (cached) | p95 latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for v in r["variants"]:
        lo, hi = v["accuracy_ci95"]
        args = f"{pct(v['args_accuracy'])} (n={v['args_n']})" if v["args_accuracy"] is not None else "-"
        ftr = pct(v["false_tool_rate"]) if v["false_tool_rate"] is not None else "-"
        mtr = pct(v["missed_tool_rate"]) if v["missed_tool_rate"] is not None else "-"
        flag = "" if v["complete"] else f" (partial: {v['scored']}/{v['planned']})"
        lines.append(
            f"| {v['variant']}{flag} | **{pct(v['accuracy'])}** | {pct(lo)} to {pct(hi)} | {args} | {ftr} | "
            f"{mtr} | {v['tool_definitions_chars']} chars, ~{v['mean_prompt_tokens']} prompt tokens | "
            f"{v['tokens_used']:,} ({pct(v['cached_share'])}) | {v['latency_p95_ms']:.0f} ms |"
        )
    if r["best"]:
        best = next(v for v in r["variants"] if v["variant"] == r["best"])
        verdict = "meets" if best["accuracy"] >= TARGET_ACCURACY else "misses"
        lines += ["", f"Best: **{r['best']}**, which {verdict} the {pct(TARGET_ACCURACY)} target."]
    if r["comparisons"]:
        lines += ["", "| A vs B | only A right | only B right | McNemar p |", "|---|---|---|---|"]
        lines += [
            f"| {c['a']} vs {c['b']} | {c['only_a_right']} | {c['only_b_right']} | {c['p_value']} |"
            for c in r["comparisons"]
        ]
    for v in r["variants"]:
        if v["confusions"]:
            lines += [
                "",
                f"Mistakes under **{v['variant']}**: " + "; ".join(f"{k} ({n})" for k, n in v["confusions"]),
            ]
    return "\n".join(lines) + "\n"
