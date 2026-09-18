"""The DNS baseline lifecycle, end to end: capture, drift, accept.

This is the one check whose incident a human has to close, so the whole path
through the runner, the database and the API is worth exercising.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from tests.integration.conftest import DATABASE_URL
from tests.integration.test_runner_incidents import RecordingNotifier
from tests.unit.test_dns_and_email_auth import BASELINE, FakeResolver, resolver_from
from tideline.api.app import create_app
from tideline.checks import Clients
from tideline.checks.http import PageFetcher
from tideline.config import Settings
from tideline.db.baselines import get_baseline
from tideline.db.models import Check, DnsBaseline, Incident
from tideline.sites import SitesFile, seed
from tideline.worker.runner import Runner

TOKEN = "test-token"


@pytest.fixture
async def dns_site(sessionmaker) -> int:
    async with sessionmaker() as session, session.begin():
        await seed(
            session,
            SitesFile.model_validate(
                {
                    "sites": [
                        {
                            "name": "Example",
                            "domain": "example.ca",
                            "checks": {
                                "uptime": False,
                                "content": False,
                                "tls": False,
                                "domain": False,
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
        check = await session.scalar(select(Check).where(Check.kind == "dns"))
        return check.id


def runner_with(sessionmaker, notifier, clock, records) -> Runner:
    clients = Clients(
        http=httpx.AsyncClient(),
        pages=PageFetcher(httpx.AsyncClient(), cache_seconds=0),
        now=clock,
        resolver=resolver_from(records) if isinstance(records, dict) else records,
    )
    return Runner(sessionmaker, clients, notifier)


@pytest.fixture
async def client(sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        Settings(
            database_url=DATABASE_URL or "",
            api_token=TOKEN,
            dashboard_password="x",
        )
    )
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
        app.router.lifespan_context(app),
    ):
        yield client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_capture_then_drift_then_accept(sessionmaker, clock, client, dns_site):
    notifier = RecordingNotifier()

    # 1. First run: nothing to compare against, so the records become the baseline.
    first = await runner_with(sessionmaker, notifier, clock, BASELINE).run_check(dns_site)
    assert first.status == "ok"
    async with sessionmaker() as session:
        site_id = (await session.get(Check, dns_site)).site_id
        assert await get_baseline(session, site_id) == BASELINE
    assert notifier.sent == []

    # 2. Someone repoints the A record: an incident opens immediately.
    moved = {**BASELINE, "example.ca": {**BASELINE["example.ca"], "A": ["203.0.113.9"]}}
    second = await runner_with(sessionmaker, notifier, clock, moved).run_check(dns_site)
    assert second.status == "fail"
    async with sessionmaker() as session:
        [incident] = list(await session.scalars(select(Incident)))
        assert incident.resolved_at is None
        assert "203.0.113.9" in incident.summary
    assert [m.kind for m in notifier.sent] == ["open"]

    # 3. The drift is still there on the next run, and an ok result would NOT
    #    close it. Run again with the same records: still one open incident.
    await runner_with(sessionmaker, notifier, clock, moved).run_check(dns_site)
    async with sessionmaker() as session:
        assert (
            await session.scalar(select(Incident).where(Incident.resolved_at.is_(None)))
        ) is not None

    # 4. Owen accepts the change: new baseline, incident closed.
    response = await client.post(f"/sites/{site_id}/dns-baseline/accept", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["incidents_resolved"] == 1
    assert body["records"]["example.ca"]["A"] == ["203.0.113.9"]

    async with sessionmaker() as session:
        assert await get_baseline(session, site_id) == moved
        assert (
            await session.scalar(select(Incident).where(Incident.resolved_at.is_(None)))
        ) is None
        # The old baseline is kept as history.
        assert len(list(await session.scalars(select(DnsBaseline)))) == 2

    # 5. The next run is quiet again.
    fifth = await runner_with(sessionmaker, notifier, clock, moved).run_check(dns_site)
    assert fifth.status == "ok"


async def test_accepting_with_no_dns_result_yet_is_a_conflict(sessionmaker, client, dns_site):
    async with sessionmaker() as session:
        site_id = (await session.get(Check, dns_site)).site_id
    response = await client.post(f"/sites/{site_id}/dns-baseline/accept", headers=auth())
    assert response.status_code == 409


async def test_accept_needs_a_token_and_a_real_site(client, dns_site):
    assert (await client.post("/sites/1/dns-baseline/accept")).status_code == 401
    assert (await client.post("/sites/9999/dns-baseline/accept", headers=auth())).status_code == 404


async def test_lookup_failure_writes_nothing(sessionmaker, clock, dns_site):
    notifier = RecordingNotifier()
    runner = runner_with(sessionmaker, notifier, clock, FakeResolver({}, fail="unavailable"))
    assert await runner.run_check(dns_site) is None
    async with sessionmaker() as session:
        assert list(await session.scalars(select(DnsBaseline))) == []
        assert list(await session.scalars(select(Incident))) == []


async def test_the_dashboard_button_accepts_only_from_the_dashboard(
    sessionmaker, clock, client, dns_site
):
    """The accept button on the site page, and its cross-site request check."""
    notifier = RecordingNotifier()
    await runner_with(sessionmaker, notifier, clock, BASELINE).run_check(dns_site)
    moved = {**BASELINE, "example.ca": {**BASELINE["example.ca"], "A": ["203.0.113.9"]}}
    await runner_with(sessionmaker, notifier, clock, moved).run_check(dns_site)
    async with sessionmaker() as session:
        site_id = (await session.get(Check, dns_site)).site_id

    dash = ("owen", "x")
    page = await client.get(f"/sites/{site_id}/view", auth=dash)
    assert "DNS has changed" in page.text
    form = f"/sites/{site_id}/dns-baseline/accept-form"
    assert f'action="{form}"' in page.text

    # A page on another site making Owen's browser press the button: refused.
    for headers in ({"Origin": "https://evil.example"}, {}):
        refused = await client.post(form, auth=dash, headers=headers)
        assert refused.status_code == 403
    async with sessionmaker() as session:
        assert await get_baseline(session, site_id) == BASELINE

    # The button itself, from the dashboard's own page: accepted, back to the site.
    accepted = await client.post(form, auth=dash, headers={"Origin": "http://t"})
    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/sites/{site_id}/view"
    async with sessionmaker() as session:
        assert await get_baseline(session, site_id) == moved
        assert (
            await session.scalar(select(Incident).where(Incident.resolved_at.is_(None)))
        ) is None

    # And it still needs the dashboard password.
    assert (await client.post(form, headers={"Origin": "http://t"})).status_code == 401
