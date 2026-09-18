"""End to end through the runner: real HTTP server, real SQLite, incident lifecycle.

A site goes down, an incident opens on the first failed run, alerts are
recorded, and it resolves on recovery. Then the same through a whole scheduled
run (tideline.worker.run), including the one summary email it sends.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.conftest import FakeClock
from tideline.checks import REGISTRY, Clients
from tideline.checks.http import PageFetcher
from tideline.db.models import Alert, Check, CheckResult, Incident
from tideline.notify.base import AlertMessage
from tideline.sites import SitesFile, seed
from tideline.worker.runner import Runner


class RecordingNotifier:
    channel = "test"

    def __init__(self) -> None:
        self.sent: list[AlertMessage] = []
        self.reports: list[str] = []
        self.fail_next = 0

    async def send_report(self, subject: str, text: str, html: str) -> str:
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("email provider down")
        self.reports.append(subject)
        return "test"

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
                                "links": False,
                                "form": False,
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
    failed_at = clock.current + timedelta(days=14)
    assert (await tick(runner, clock, uptime, minutes=14 * 24 * 60)).status == "fail"
    # Runs are two weeks apart, so the first failed run opens the incident
    # (the check itself already retried once before failing).
    [incident] = await incidents(sessionmaker)
    assert incident.resolved_at is None
    assert incident.severity == "critical"
    assert incident.opened_at == failed_at
    assert [m.kind for m in notifier.sent] == ["open"]

    # Still down at the next run: no reminder, the run summary lists it instead.
    await tick(runner, clock, uptime, minutes=14 * 24 * 60)
    assert [m.kind for m in notifier.sent] == ["open"]

    await fake_site.start()  # same port
    assert (await tick(runner, clock, uptime, minutes=14 * 24 * 60)).status == "ok"
    [incident] = await incidents(sessionmaker)
    assert incident.resolved_at == clock.current
    assert [m.kind for m in notifier.sent] == ["open", "resolved"]
    assert notifier.sent[-1].duration == incident.resolved_at - incident.opened_at
    assert await alert_kinds(sessionmaker) == ["open", "resolved"]


async def test_content_failure_opens_immediately(sessionmaker, runner, notifier, clock, fake_site):
    ids = await seed_fake(sessionmaker, fake_site.url)
    fake_site.body = "<h1>This account has been suspended</h1>"
    result = await tick(runner, clock, ids["content"])
    assert result.status == "fail"
    [incident] = await incidents(sessionmaker)
    assert incident.summary == 'The page no longer shows "Fake Bakery"'
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


# --- a whole run --------------------------------------------------------------------


def run_settings():
    from tests.integration.conftest import DATABASE_URL
    from tideline.config import Settings

    return Settings(database_url=DATABASE_URL, uptime_retry_delay_seconds=0)


async def test_a_run_emails_only_when_something_changes(sessionmaker, fake_site):
    from tideline.db.models import DailyRollup
    from tideline.worker.run import run

    await seed_fake(sessionmaker, fake_site.url)
    notifier = RecordingNotifier()
    settings = run_settings()

    first = await run(settings, notifier, reports=False)
    assert (first.checks, first.failures, first.summary_emailed) == (2, 0, False)
    assert notifier.reports == []  # all fine: no email at all

    await fake_site.stop()
    down = await run(settings, notifier, reports=False)
    assert down.summary_emailed
    assert notifier.reports == ["Tideline check: 1 new problem"]
    assert down.open_incidents == 1

    still_down = await run(settings, notifier, reports=False)
    assert not still_down.summary_emailed  # nothing new: no second email
    assert len(notifier.reports) == 1

    await fake_site.start()
    fixed = await run(settings, notifier, reports=False)
    assert fixed.summary_emailed
    assert notifier.reports[-1] == "Tideline check: 1 fixed"
    assert fixed.open_incidents == 0

    async with sessionmaker() as session:
        [rollup] = list(await session.scalars(select(DailyRollup)))
    assert rollup.uptime_checks == 4  # every run on the same test day, one row


async def test_a_run_on_the_first_sends_the_monthly_reports(sessionmaker, fake_site):
    from datetime import UTC, datetime

    from tideline.worker.run import run

    await seed_fake(sessionmaker, fake_site.url)
    notifier = RecordingNotifier()
    # 1 October, 07:00 in Vancouver.
    report = await run(run_settings(), notifier, now=datetime(2026, 10, 1, 14, 0, tzinfo=UTC))
    assert report.reports_sent == 1
    assert notifier.reports == ["September 2026 report: Fake Bakery"]


async def test_a_run_loads_the_site_list_first(sessionmaker, tmp_path, fake_site):
    from tideline.worker.run import run

    sites = tmp_path / "sites.yaml"
    sites.write_text(
        f"""
sites:
  - name: Fake Bakery
    domain: fakesite.test
    expected_text: Fake Bakery
    urls: ["{fake_site.url}"]
    checks: {{tls: false, domain: false, dns: false, email_auth: false, links: false, form: false}}
"""
    )
    report = await run(run_settings(), RecordingNotifier(), sites_file=sites, reports=False)
    assert report.checks == 2


async def test_a_run_can_be_narrowed_to_one_kind(sessionmaker, fake_site):
    from tideline.worker.run import run

    await seed_fake(sessionmaker, fake_site.url)
    report = await run(run_settings(), RecordingNotifier(), kind="content", reports=False)
    assert report.checks == 1
