"""Gateway rate limits and quotas.

Three limits per call, all must pass:
- per user per minute (one person can't drain an app's budget)
- per client app per minute (one app can't starve the others)
- per client app per UTC day (the commercial quota)

Token buckets refill continuously, so there is no burst at the top of each minute. A refused
call gets Retry-After and the IETF RateLimit-* headers so well-behaved agents can back off.

The store is in memory. Running several gateway replicas would need a shared store (Redis)
behind the same `RateLimiter` interface.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit_name: str  # which limit decided: "user_minute", "client_minute", "client_day"
    limit: int
    remaining: int
    retry_after: int  # seconds, 0 when allowed

    def headers(self) -> dict[str, str]:
        h = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.retry_after),
        }
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens, self.updated = capacity, now


class TokenBuckets:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def _refill(self, key: str, per_minute: int) -> _Bucket:
        now = self.clock()
        b = self._buckets.get(key)
        if b is None:
            b = self._buckets[key] = _Bucket(per_minute, now)
        else:
            b.tokens = min(per_minute, b.tokens + (now - b.updated) * per_minute / 60.0)
            b.updated = now
        return b

    def peek(self, key: str, per_minute: int) -> tuple[bool, int, int]:
        """(has a token, tokens left, seconds until one is free) without spending."""
        b = self._refill(key, per_minute)
        if b.tokens >= 1:
            return True, int(b.tokens), 0
        return False, 0, max(1, math.ceil((1 - b.tokens) * 60.0 / per_minute))

    def spend(self, key: str) -> None:
        self._buckets[key].tokens -= 1


class DailyQuota:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self._used: dict[tuple[str, str], int] = {}

    def _day(self) -> tuple[str, int]:
        now = datetime.fromtimestamp(self.clock(), UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86_400
        return now.date().isoformat(), max(1, math.ceil(midnight - self.clock()))

    def peek(self, key: str, quota: int) -> tuple[bool, int, int]:
        day, until_reset = self._day()
        used = self._used.get((key, day), 0)
        return used < quota, max(0, quota - used), 0 if used < quota else until_reset

    def spend(self, key: str) -> None:
        day, _ = self._day()
        self._used[(key, day)] = self._used.get((key, day), 0) + 1
        # forget earlier days
        for k in [k for k in self._used if k[1] != day]:
            del self._used[k]


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.minute = TokenBuckets(clock)
        self.daily = DailyQuota(clock)

    def check(
        self,
        client_id: str,
        subject: str,
        *,
        per_user_per_minute: int,
        per_client_per_minute: int,
        daily_quota: int,
    ) -> RateDecision:
        """Spend from every limit only if all of them have room, so a refusal costs nothing."""
        checks = [
            (
                "user_minute",
                per_user_per_minute,
                self.minute.peek(f"u:{client_id}:{subject}", per_user_per_minute),
            ),
            (
                "client_minute",
                per_client_per_minute,
                self.minute.peek(f"c:{client_id}", per_client_per_minute),
            ),
            ("client_day", daily_quota, self.daily.peek(client_id, daily_quota)),
        ]
        for name, limit, (ok, remaining, retry) in checks:
            if not ok:
                return RateDecision(False, name, limit, remaining, retry)
        self.minute.spend(f"u:{client_id}:{subject}")
        self.minute.spend(f"c:{client_id}")
        self.daily.spend(client_id)
        name, limit, (_, remaining, _) = min(checks, key=lambda c: c[2][1])
        return RateDecision(True, name, limit, max(0, remaining - 1), 0)
