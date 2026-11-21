from pathlib import Path

import pytest
from pydantic import ValidationError

from crewgate.core.entitlements import Entitlement, EntitlementError, EntitlementFailure, EntitlementPolicy

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def policy() -> EntitlementPolicy:
    return EntitlementPolicy.from_toml(REPO / "config" / "entitlements.toml")


def reason(exc: pytest.ExceptionInfo[EntitlementError]) -> EntitlementFailure:
    return exc.value.reason


def test_shipped_config_loads(policy: EntitlementPolicy) -> None:
    agent = policy.get("crewgate-mcp-client")
    assert agent.allows_tool("crew.get_roster") and agent.allows_tool("booking.search_flights")
    assert agent.allows_market("se") and not agent.allows_market("US")


def test_unknown_client_is_refused(policy: EntitlementPolicy) -> None:
    with pytest.raises(EntitlementError) as e:
        policy.check("someone-else", "crew.get_roster")
    assert reason(e) is EntitlementFailure.UNKNOWN_CLIENT


def test_booking_assistant_gets_no_crew_data(policy: EntitlementPolicy) -> None:
    with pytest.raises(EntitlementError) as e:
        policy.check("booking-assistant-se", "crew.list_crew")
    assert reason(e) is EntitlementFailure.TOOL_NOT_ENTITLED


def test_exact_tool_list_does_not_match_siblings(policy: EntitlementPolicy) -> None:
    policy.check("roster-dashboard", "crew.get_roster")
    with pytest.raises(EntitlementError):
        policy.check("roster-dashboard", "crew.propose_swap")


def test_markets(policy: EntitlementPolicy) -> None:
    policy.check("booking-assistant-se", "booking.search_flights", "SE", market_bound=True)
    with pytest.raises(EntitlementError) as e:
        policy.check("booking-assistant-se", "booking.search_flights", "NO", market_bound=True)
    assert reason(e) is EntitlementFailure.MARKET_NOT_ENTITLED
    with pytest.raises(EntitlementError) as e:
        policy.check("booking-assistant-se", "booking.search_flights", None, market_bound=True)
    assert reason(e) is EntitlementFailure.MARKET_REQUIRED
    policy.check("roster-dashboard", "crew.get_roster", "US")  # "*" means any market


def test_config_is_strict() -> None:
    with pytest.raises(ValidationError):
        EntitlementPolicy.from_dict({"clients": {"x": {"tools": ["a"], "typo_field": 1}}})
    with pytest.raises(ValidationError):
        Entitlement(client_id="x", tools=("a",), per_user_per_minute=0)
