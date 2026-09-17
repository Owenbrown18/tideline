"""End to end through the runner: real HTTP server, real Postgres, incident lifecycle.

This is the M1 "Done when" scenario as a test: a site goes down, an incident
opens after two failed runs, alerts are recorded, and it resolves on recovery.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from sitewatch.checks import REGISTRY, Clients
from sitewatch.checks.http import PageFetcher
from sitewatch.db.models import Alert, Check, CheckResult, Incident
from sitewatch.notify.base import AlertMessage
from sitewatch.sites import SitesFile, seed
from sitewatch.worker.runner import Runner
from sitewatch.worker.scheduler import JOB_PREFIX, Worker, first_run_offset
from tests.conftest import FakeClock


class RecordingNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []
        self.fail_next = 0

    async def send(self, msg: AlertMessage) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("email provider down")
        self.sent.append(msg)


class FakeSite:
    """A minimal HTTP server on localhost that can be stopped and started."""

    def __init__(self, body: str = "<title>Fake Bakery</title>") -> None:
        self.body = body
        self.port = 0
        self._server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        payload = self.body.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


@pytest.fixture
async def fake_site() -> AsyncIterator[FakeSite]:
    site = FakeSite()
    await site.start()
    yield site


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
async def runner(sessionmaker, notifier, clock, sleeps) -> AsyncIterator[Runner]:
    async with httpx.AsyncClient() as http:
        clients = Clients(
            http=http, pages=PageFetcher(http, cache_seconds=0), now=clock, sleep=sleeps
        )
        yield Runner(sessionmaker, clients, notifier)


async def seed_fake(sessionmaker, url: str) -> dict[str, int]:
    async with sessionmaker() as session, session.begin():
        await seed(
            session,
            SitesFile.model_validate(
                {
                    "sites": [
                        {
                            "name": "Fake Bakery",
                            "domain": "fakesite.test",
                            "expected_text": "Fake Bakery",
                            "urls": [url],
                            "checks": {
                                "tls": False,
                                "domain": False,
                                "dns": False,
                                "email_auth": False,
                            },
                        }
                    ]
                }
            ),
        )
    async with sessionmaker() as session:
        return {c.kind: c.id for c in await session.scalars(select(Check))}


async def incidents(sessionmaker) -> list[Incident]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Incident).order_by(Incident.id)))


async def alert_kinds(sessionmaker) -> list[str]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(Alert.kind).order_by(Alert.id)))


async def tick(runner: Runner, clock: FakeClock, check_id: int, minutes: int = 5):
    clock.current += timedelta(minutes=minutes)
    return await runner.run_check(check_id)


async def test_site_down_opens_then_recovery_resolves(
    sessionmaker, runner, notifier, clock, fake_site
):
    ids = await seed_fake(sessionmaker, fake_site.url)
    uptime = ids["uptime"]

    assert (await tick(runner, clock, uptime)).status == "ok"
    assert await incidents(sessionmaker) == []

    await fake_site.stop()
    first_failure_at = clock.current + timedelta(minutes=5)
    assert (await tick(runner, clock, uptime)).status == "fail"
    assert await incidents(sessionmaker) == []  # one failure is not an incident

    assert (await tick(runner, clock, uptime)).status == "fail"
    [incident] = await incidents(sessionmaker)
    assert incident.resolved_at is None
    assert incident.severity == "critical"
    assert incident.opened_at == first_failure_at  # dated from the first failure
    assert [m.kind for m in notifier.sent] == ["open"]

    # Still down 5 minutes later: no repeat alert.
    await tick(runner, clock, uptime)
    assert len(notifier.sent) == 1

    # Still down a day later: one reminder.
    await tick(runner, clock, uptime, minutes=24 * 60)
    assert [m.kind for m in notifier.sent] == ["open", "reminder"]

    await fake_site.start()  # same port
    assert (await tick(runner, clock, uptime)).status == "ok"
    [incident] = await incidents(sessionmaker)
    assert incident.resolved_at == clock.current
    assert [m.kind for m in notifier.sent] == ["open", "reminder", "resolved"]
    assert notifier.sent[-1].duration == incident.resolved_at - incident.opened_at
    assert await alert_kinds(sessionmaker) == ["open", "reminder", "resolved"]


async def test_content_failure_opens_immediately(sessionmaker, runner, notifier, clock, fake_site):
    ids = await seed_fake(sessionmaker, fake_site.url)
    fake_site.body = "<h1>This account has been suspended</h1>"
    result = await tick(runner, clock, ids["content"])
    assert result.status == "fail"
    [incident] = await incidents(sessionmaker)
    assert "expected text" in incident.summary
    assert notifier.sent[0].check_kind == "content"


async def test_content_check_stays_quiet_when_site_is_down(sessionmaker, runner, clock, fake_site):
    ids = await seed_fake(sessionmaker, fake_site.url)
    await fake_site.stop()
    assert await tick(runner, clock, ids["content"]) is None
    async with sessionmaker() as session:
        assert list(await session.scalars(select(CheckResult))) == []


async def test_failed_alert_is_retried_on_the_next_run(
    sessionmaker, runner, notifier, clock, fake_site
):
    ids = await seed_fake(sessionmaker, fake_site.url)
    await fake_site.stop()
    notifier.fail_next = 1

    await tick(runner, clock, ids["uptime"])
    await tick(runner, clock, ids["uptime"])  # opens, but the send fails
    [incident] = await incidents(sessionmaker)
    assert incident.last_alerted_at is None
    assert await alert_kinds(sessionmaker) == []

    await tick(runner, clock, ids["uptime"])  # retried
    assert [m.kind for m in notifier.sent] == ["open"]
    assert await alert_kinds(sessionmaker) == ["open"]


@respx.mock
async def test_crashing_check_records_nothing_and_counts_an_error(
    sessionmaker, runner, clock, monkeypatch
):
    ids = await seed_fake(sessionmaker, "https://fakesite.test/")

    async def broken(config, clients):
        raise KeyError("bug")

    monkeypatch.setitem(REGISTRY, "uptime", broken)
    assert await tick(runner, clock, ids["uptime"]) is None
    assert runner.stats["check_errors"] == 1
    assert await incidents(sessionmaker) == []


async def test_worker_schedules_enabled_checks_and_follows_changes(sessionmaker, engine, fake_site):
    from sitewatch.config import Settings
    from tests.integration.conftest import DATABASE_URL

    ids = await seed_fake(sessionmaker, fake_site.url)
    worker = Worker(Settings(database_url=DATABASE_URL))
    worker.scheduler.start(paused=True)
    try:
        await worker.refresh_jobs()
        scheduled = {j.id for j in worker.scheduler.get_jobs()}
        assert scheduled == {f"{JOB_PREFIX}{ids['uptime']}", f"{JOB_PREFIX}{ids['content']}"}

        async with sessionmaker() as session, session.begin():
            check = await session.get(Check, ids["content"])
            check.enabled = False
            uptime = await session.get(Check, ids["uptime"])
            uptime.interval_seconds = 42
        await worker.refresh_jobs()
        [job] = worker.scheduler.get_jobs()
        assert job.id == f"{JOB_PREFIX}{ids['uptime']}"
        assert job.trigger.interval == timedelta(seconds=42)

        await worker.heartbeat()  # runs against the real database without error
    finally:
        worker.scheduler.shutdown(wait=False)
        await worker.http.aclose()
        await worker.engine.dispose()


def test_first_runs_are_spread_over_a_minute():
    offsets = {first_run_offset(i, 300) for i in range(1, 50)}
    assert min(offsets) >= 0
    assert max(offsets) < 60
    assert len(offsets) > 20
    assert first_run_offset(5, 10) < 10
