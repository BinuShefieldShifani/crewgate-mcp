"""Crew plugin client against the strict mock partner: pagination, rate limits, token lifecycle."""

import pytest

from crewgate.core.errors import ErrorCategory, PartnerError


async def test_pagination_returns_every_crew_member_once(crew_env):
    async with crew_env() as client:
        ids = [c["crew_id"] async for c in client.iter_crew(page_size=25)]
    assert len(ids) == 120 and len(set(ids)) == 120


async def test_backs_off_on_429_and_finishes_when_the_budget_allows(crew_env):
    # a batch-style caller that can afford to wait out the partner's one-minute window
    client = crew_env(api={"rate_limit_per_minute": 2}, crew={"max_retry_after_seconds": 120})
    ids = [c["crew_id"] async for c in client.iter_crew(page_size=40)]  # 3 calls, limit 2/min
    assert len(ids) == 120
    waits = [e.waited for e in client.partner.retry_log if e.status == 429]
    assert waits and all(w >= 1 for w in waits)


async def test_interactive_default_fails_fast_instead_of_waiting_30s(crew_env):
    client = crew_env(api={"rate_limit_per_minute": 2})
    with pytest.raises(PartnerError) as e:
        _ = [c async for c in client.iter_crew(page_size=40)]
    assert e.value.category is ErrorCategory.RATE_LIMITED
    assert client.partner.retry_log == []  # it did not sit and wait


async def test_gives_up_after_retry_budget_with_category(crew_env):
    client = crew_env(api={"rate_limit_per_minute": 1})
    client.partner.policy.max_retries = 0
    await client.get_roster("C0001")
    with pytest.raises(PartnerError) as e:
        await client.get_roster("C0002")
    assert e.value.category is ErrorCategory.RATE_LIMITED and e.value.retryable


async def test_token_reused_while_fresh(crew_env):
    client = crew_env()
    for cid in ("C0001", "C0002", "C0003"):
        await client.get_roster(cid)
    assert client.auth.fetch_count == 1


async def test_token_refreshed_before_expiry(crew_env, clock):
    client = crew_env(api={"token_ttl_seconds": 120})
    await client.get_roster("C0001")
    clock.t += 95  # inside the 30 s refresh margin; token still technically valid
    await client.get_roster("C0002")
    assert client.auth.fetch_count == 2


async def test_rejected_token_triggers_exactly_one_refresh(crew_env):
    client = crew_env()
    await client.get_roster("C0001")
    client.auth._token = "revoked.or.rotated"
    await client.get_roster("C0002")
    assert client.auth.fetch_count == 2


async def test_missing_scope_maps_to_forbidden_without_retry(crew_env):
    client = crew_env(crew={"scopes": "roster.read"})
    with pytest.raises(PartnerError) as e:
        await client.request_swap("C0001", "C0002", "2026-12-03")
    assert e.value.category is ErrorCategory.FORBIDDEN and client.partner.retry_log == []


async def test_bad_credentials_map_to_auth(crew_env):
    client = crew_env(crew={"client_secret": "wrong"})
    with pytest.raises(PartnerError) as e:
        await client.get_roster("C0001")
    assert e.value.category is ErrorCategory.AUTH


async def test_swap_lands_as_pending_approval(crew_env):
    client = crew_env()
    swap = await client.request_swap("C0001", "C0002", "2026-12-03")
    assert swap["status"] == "pending_approval"
