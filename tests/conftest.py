"""Fixtures shared by unit and integration tests."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import httpx
import pytest

from sitewatch.checks import Clients
from sitewatch.checks.http import PageFetcher

FIXED_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class FakeClock:
    """A controllable `now()` for tests that step through time."""

    def __init__(self, start: datetime = FIXED_NOW) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def make_clients(
    http: httpx.AsyncClient, clock: FakeClock, sleeps: SleepRecorder
) -> Callable[..., Clients]:
    def factory(**overrides: object) -> Clients:
        values: dict[str, object] = {
            "http": http,
            # cache_seconds=0 unless a test is about caching: each fetch is a real request.
            "pages": PageFetcher(http, cache_seconds=0),
            "now": clock,
            "sleep": sleeps,
            "uptime_retry_delay_seconds": 30.0,
            "rdap_base_url": "https://rdap.test/domain/",
        }
        values.update(overrides)
        return Clients(**values)  # type: ignore[arg-type]

    return factory
