"""Deterministic synthetic crew and roster data. Every name is invented.

Modelled on how airline crew rosters are usually structured: crew members with a rank and
a home base, and one duty per day (flight, standby, day off, training).
"""

import random
from datetime import date, timedelta
from typing import Any

FIRST = ["Anna", "Erik", "Sara", "Johan", "Maria", "Lars", "Emma", "Oskar", "Lina", "Nils",
         "Karin", "Per", "Elin", "Anders", "Frida", "Mikael"]  # fmt: skip
LAST = ["Berg", "Lind", "Holm", "Ek", "Strand", "Dahl", "Sjoberg", "Nyman", "Falk", "Wikstrom"]
RANKS = ["Captain", "First Officer", "Purser", "Cabin Crew"]
BASES = ["GOT", "ARN", "CPH", "OSL"]
DUTIES = ["FLT", "FLT", "FLT", "STBY", "OFF", "OFF", "TRN"]
ROUTES = ["GOT-ARN", "GOT-CPH", "ARN-OSL", "CPH-LHR", "GOT-AMS", "ARN-HEL"]

ROSTER_START = date(2026, 12, 1)
ROSTER_DAYS = 14


def build_dataset(
    n_crew: int = 120, seed: int = 42
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rng = random.Random(seed)  # noqa: S311  # nosec B311 synthetic data, not crypto
    crew: list[dict[str, Any]] = []
    roster: dict[str, list[dict[str, Any]]] = {}
    for i in range(1, n_crew + 1):
        cid = f"C{i:04d}"
        crew.append(
            {
                "crew_id": cid,
                "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                "rank": rng.choice(RANKS),
                "base": rng.choice(BASES),
                "email": f"{cid.lower()}@crew.example",
            }
        )
        days = []
        for d in range(ROSTER_DAYS):
            duty = rng.choice(DUTIES)
            days.append(
                {
                    "date": (ROSTER_START + timedelta(days=d)).isoformat(),
                    "duty": duty,
                    "route": rng.choice(ROUTES) if duty == "FLT" else None,
                }
            )
        roster[cid] = days
    return crew, roster
