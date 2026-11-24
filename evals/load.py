"""Load test: tool calls per second and latency at rising concurrency.

Everything runs in one process on one core: the load generator, the gateway, the fake IdP and
the fake partners share an event loop. So the numbers are a floor for the gateway's own code
path (auth check, entitlements, audit, signing, MCP framing), not a capacity figure for a
deployment. Each worker holds its own token, and the limits are raised for this run only
(datasets/load_entitlements.toml): the rate limiter is tested in the offline evals.

  crew_get_roster       token exchange (cached), HMAC-signed call to the crew API
  booking_get_seat_map  Duffel call with the gateway's own key
"""

import asyncio
import time
from pathlib import Path
from typing import Any

from tests.helpers.stack import Stack, gateway_stack

from .common import percentile
from .offline import body_of, search, tool

ENTITLEMENTS = Path(__file__).parent / "datasets" / "load_entitlements.toml"
LEVELS = (1, 8, 32)


async def _level(st: Stack, name: str, args: dict[str, Any], concurrency: int, calls: int) -> dict[str, Any]:
    tokens = [st.agent_token() for _ in range(concurrency)]  # one token per worker, all anna's
    await tool(st, name, args, tokens[0])  # warm up: JWKS fetch, first token exchange
    latencies: list[float] = []
    errors = 0
    per_worker = max(1, calls // concurrency)

    async def worker(token: str) -> None:
        nonlocal errors
        for _ in range(per_worker):
            started = time.perf_counter()
            result = await tool(st, name, args, token)
            latencies.append((time.perf_counter() - started) * 1000)
            errors += bool(result.get("isError"))

    started = time.perf_counter()
    await asyncio.gather(*(worker(t) for t in tokens))
    wall = time.perf_counter() - started
    n = per_worker * concurrency
    return {
        "tool": name,
        "concurrency": concurrency,
        "calls": n,
        "errors": errors,
        "calls_per_second": round(n / wall, 1),
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
    }


async def run_load(calls: int = 320, levels: tuple[int, ...] = LEVELS) -> dict[str, Any]:
    rows = []
    async with gateway_stack(entitlements=ENTITLEMENTS) as st:
        offer = body_of(await search(st, "ARN", "LHR"))["offers"][0]
        cases = [
            ("crew_get_roster", {"crew_id": "C0005"}),
            ("booking_get_seat_map", {"offer_id": offer["offer_id"]}),
        ]
        for name, args in cases:
            for level in levels:
                rows.append(await _level(st, name, args, level, calls))
    return {"suite": "load", "passed": all(r["errors"] == 0 for r in rows), "rows": rows}


def render_load(r: dict[str, Any]) -> str:
    lines = [
        "## Load (one process, one core: a floor for the gateway's own code path)",
        "",
        "| Tool | Concurrency | Calls | Errors | Calls/s | p50 | p95 | p99 |",
        "|---|---|---|---|---|---|---|---|",
        *[
            f"| {x['tool']} | {x['concurrency']} | {x['calls']} | {x['errors']} | {x['calls_per_second']} | "
            f"{x['p50_ms']} ms | {x['p95_ms']} ms | {x['p99_ms']} ms |"
            for x in r["rows"]
        ],
    ]
    return "\n".join(lines) + "\n"
