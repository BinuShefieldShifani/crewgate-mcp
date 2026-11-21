from datetime import UTC, datetime

from crewgate.core.ratelimit import RateLimiter


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


NOON = datetime(2026, 12, 1, 12, 0, tzinfo=UTC).timestamp()
LIMITS = {"per_user_per_minute": 3, "per_client_per_minute": 5, "daily_quota": 8}


def test_user_limit_then_refill() -> None:
    clock = Clock(NOON)
    rl = RateLimiter(clock)
    assert [rl.check("app", "anna", **LIMITS).allowed for _ in range(4)] == [True, True, True, False]
    refused = rl.check("app", "anna", **LIMITS)
    assert refused.limit_name == "user_minute"
    assert refused.retry_after == 20  # one token every 60/3 = 20 s
    assert refused.headers()["Retry-After"] == "20"
    clock.t += 20
    assert rl.check("app", "anna", **LIMITS).allowed


def test_client_limit_protects_other_apps() -> None:
    rl = RateLimiter(Clock(NOON))
    for user in ("anna", "erik", "maria", "lars", "emma"):
        assert rl.check("app", user, **LIMITS).allowed
    refused = rl.check("app", "nils", **LIMITS)
    assert not refused.allowed and refused.limit_name == "client_minute"
    assert rl.check("other-app", "nils", **LIMITS).allowed


def test_daily_quota_resets_at_utc_midnight() -> None:
    clock = Clock(NOON)
    rl = RateLimiter(clock)
    for i in range(8):
        clock.t += 60  # stay under the minute limits
        assert rl.check("app", f"u{i}", **LIMITS).allowed
    clock.t += 60
    refused = rl.check("app", "late", **LIMITS)
    assert refused.limit_name == "client_day"
    midnight = datetime(2026, 12, 2, tzinfo=UTC).timestamp()
    assert refused.retry_after == int(midnight - clock.t)
    clock.t = midnight + 1
    assert rl.check("app", "late", **LIMITS).allowed


def test_refusals_do_not_spend_budget() -> None:
    rl = RateLimiter(Clock(NOON))
    for _ in range(3):
        rl.check("app", "anna", **LIMITS)
    for _ in range(10):
        assert not rl.check("app", "anna", **LIMITS).allowed
    assert rl.check("app", "erik", **LIMITS).allowed  # client bucket still has 2 left
    assert rl.check("app", "maria", **LIMITS).allowed


def test_allowed_headers_report_the_tightest_limit() -> None:
    d = RateLimiter(Clock(NOON)).check("app", "anna", **LIMITS)
    assert d.allowed and d.limit_name == "user_minute" and d.remaining == 2
    assert "Retry-After" not in d.headers()
    assert d.headers()["RateLimit-Limit"] == "3"
