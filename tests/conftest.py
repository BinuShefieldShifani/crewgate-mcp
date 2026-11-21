import time
from collections.abc import Callable

import httpx
import pytest

from crewgate.plugins.crew.client import RosterClient
from crewgate.plugins.crew.config import CrewSettings
from mock_roster_api.app import create_app
from mock_roster_api.settings import ApiSettings


class FakeClock:
    """One clock shared by partner API and client. sleep() moves time instead of waiting."""

    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def crew_env(clock: FakeClock) -> Callable[..., RosterClient]:
    def _make(api: dict | None = None, crew: dict | None = None) -> RosterClient:
        app = create_app(ApiSettings(**(api or {})), clock=clock)
        settings = CrewSettings(api_base_url="http://mock", **(crew or {}))
        return RosterClient(settings, transport=httpx.ASGITransport(app=app), clock=clock, sleep=clock.sleep)

    return _make


@pytest.fixture
def raw_http(clock: FakeClock) -> Callable[..., httpx.AsyncClient]:
    def _make(api: dict | None = None) -> httpx.AsyncClient:
        app = create_app(ApiSettings(**(api or {})), clock=clock)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock")

    return _make
