"""Shared pieces: confidence intervals, percentiles, a call budget, and result files."""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Honest for small n, unlike p +/- 1.96*sqrt(p(1-p)/n)."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


class BudgetExhausted(Exception):
    pass


@dataclass
class Budget:
    """Hard cap on model calls for one run, under the provider's daily limit."""

    limit: int
    used: int = 0

    def take(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            raise BudgetExhausted(f"model-call budget of {self.limit} used up")
        self.used += n

    @property
    def left(self) -> int:
        return self.limit - self.used


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("//"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{n}: {exc}") from exc
    return rows
