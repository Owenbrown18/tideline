"""Daily rollups, retention, and the monthly report."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select

from sitewatch.api.app import create_app
from sitewatch.config import Settings
from sitewatch.db.models import Check, CheckResult, DailyRollup, Incident, Site
from sitewatch.reports.monthly import build_report, render_html, render_text
from sitewatch.reports.rollups import daily_maintenance, purge_old_results, rollup_day
from tests.integration.conftest import DATABASE_URL

DASH = ("owen", "test-password")
DAY = date(2026, 9, 10)
DAY_START = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.fixture
async def site_with_a_day(sessionmaker) -> dict[str, int]:
    """One site, one uptime check, a day of results with a 10-minute outage."""
    async with sessionmaker() as session, session.begin():
        site = Site(
            name="Daves' Bakery",
            domain="davesbakery.ca",
            urls=["https://davesbakery.ca/"],
            expected_text="Daves' Bakery",
        )
        session.add(site)
        await session.flush()
        uptime = Check(
            site_id=site.id,
            kind="uptime",
            key="uptime:https://davesbakery.ca/",
            interval_seconds=300,
            config={},
        )
        tls = Check(
            site_id=site.id, kind="tls", key="tls:davesbakery.ca", interval_seconds=21600, config={}
        )
        session.add_all([uptime, tls])
        await session.flush()

        # 24 hourly uptime results; two of them failed.
        for hour in range(24):
            failed = hour in (3, 4)
            session.add(
                CheckResult(
                    check_id=uptime.id,
                    started_at=DAY_START + timedelta(hours=hour),
                    duration_ms=100,
                    status="fail" if failed else "ok",
                    detail={"summary": "x", "total_ms": 0 if failed else 100 + hour},
                )
            )
        session.add(
            CheckResult(
                check_id=tls.id,
                started_at=DAY_START + timedelta(hours=6),
                duration_ms=90,
                status="ok",
                detail={"summary": "certificate valid for 60 more days"},
            )
        )
        session.add(
            Incident(
                check_id=uptime.id,
                opened_at=DAY_START + timedelta(hours=3),
                resolved_at=DAY_START + timedelta(hours=3, minutes=10),
                severity="critical",
                summary="https://davesbakery.ca/ is down: HTTP 503",
            )
        )
        await session.flush()
        return {"site": site.id, "uptime": uptime.id}


async def test_rollup_summarises_a_day(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        assert await rollup_day(session, DAY) == 1

    async with sessionmaker() as session:
        row = await session.scalar(select(DailyRollup))
        assert row.day == DAY
        assert row.results == 25  # 24 uptime plus one TLS
        assert row.uptime_checks == 24
        assert row.uptime_ok == 22
        assert row.uptime_percent == pytest.approx(91.667, abs=0.01)
        assert row.incidents_opened == 1
        assert row.downtime_seconds == 600
        assert row.p50_ms is not None


async def test_rollup_is_idempotent(sessionmaker, site_with_a_day):
    for _ in range(3):
        async with sessionmaker() as session, session.begin():
            await rollup_day(session, DAY)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(DailyRollup)) == 1


async def test_a_day_with_no_results_writes_nothing(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        assert await rollup_day(session, date(2026, 9, 11)) == 0


async def test_purge_keeps_recent_results_and_the_rollups(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)

    # 100 days after that day, the raw results are past the 90-day window.
    later = DAY_START + timedelta(days=100)
    async with sessionmaker() as session, session.begin():
        purged = await purge_old_results(session, later)
    assert purged == 25

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(CheckResult)) == 0
        # The history survives in the rollup, which is the point.
        row = await session.scalar(select(DailyRollup))
        assert row.uptime_ok == 22


async def test_daily_maintenance_rolls_up_yesterday(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        summary = await daily_maintenance(session, now=DAY_START + timedelta(days=1, hours=1))
    assert summary["sites_rolled_up"] == 1
    async with sessionmaker() as session:
        assert (await session.scalar(select(DailyRollup))).day == DAY


# --- the monthly report ----------------------------------------------------------


@pytest.fixture
async def client(sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        Settings(database_url=DATABASE_URL or "", api_token="t", dashboard_password=DASH[1])
    )
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
        app.router.lifespan_context(app),
    ):
        yield client


async def test_report_reads_from_the_rollups(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)

    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 9)

    assert report.site_name == "Daves' Bakery"
    assert report.month_name == "September 2026"
    assert report.uptime_percent == pytest.approx(91.667, abs=0.01)
    assert report.checks_run == 25
    assert report.days_covered == 1
    assert report.days_in_month == 30
    assert report.downtime_text == "10 min"
    assert len(report.incidents) == 1

    html = render_html(report)
    assert "Daves&#39; Bakery" in html
    assert "91.67%" in html
    assert "10 min" in html
    assert "is down: HTTP 503" in html

    text = render_text(report)
    assert "Uptime:          91.67%" in text
    assert "Incidents (1):" in text


async def test_report_survives_the_raw_results_being_purged(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
        await purge_old_results(session, DAY_START + timedelta(days=100))

    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 9)
    assert report.checks_run == 25
    assert report.uptime_percent == pytest.approx(91.667, abs=0.01)


async def test_a_quiet_month_says_so(sessionmaker, site_with_a_day):
    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 8)
    assert report.incidents == []
    assert report.uptime_text == "no data"
    assert "Nothing went wrong" in render_html(report)


async def test_report_page_needs_auth_and_a_real_site(client, site_with_a_day, sessionmaker):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)

    site_id = site_with_a_day["site"]
    assert (await client.get(f"/reports/{site_id}/2026-09")).status_code == 401
    assert (await client.get("/reports/9999/2026-09", auth=DASH)).status_code == 404
    assert (await client.get(f"/reports/{site_id}/nonsense", auth=DASH)).status_code == 422
    assert (await client.get(f"/reports/{site_id}/2026-13", auth=DASH)).status_code == 422

    page = await client.get(f"/reports/{site_id}/2026-09", auth=DASH)
    assert page.status_code == 200
    assert "September 2026" in page.text
    assert "91.67%" in page.text
