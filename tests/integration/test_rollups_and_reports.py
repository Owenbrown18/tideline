"""Daily rollups, retention, and the monthly report."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select

from tests.integration.conftest import DATABASE_URL
from tideline.api.app import create_app
from tideline.config import Settings
from tideline.db.models import Check, CheckResult, DailyRollup, Incident, Site
from tideline.reports.monthly import build_report, render_html, render_text
from tideline.reports.rollups import after_run, purge_old_results, rollup_day

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

    # 401 days after that day, the raw results are past the 400-day window.
    later = DAY_START + timedelta(days=401)
    async with sessionmaker() as session, session.begin():
        purged = await purge_old_results(session, later)
    assert purged == 25

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(CheckResult)) == 0
        # The history survives in the rollup, which is the point.
        row = await session.scalar(select(DailyRollup))
        assert row.uptime_ok == 22


async def test_after_run_rolls_up_the_run_day(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        summary = await after_run(session, now=DAY_START + timedelta(hours=23))
    assert summary["sites_rolled_up"] == 1
    async with sessionmaker() as session:
        assert (await session.scalar(select(DailyRollup))).day == DAY


# --- the monthly report ----------------------------------------------------------


@pytest.fixture
async def client(sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=DATABASE_URL, api_token="t", dashboard_password=DASH[1]))
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
    assert report.uptime_text == "0 of 1"  # two of that day's checks failed
    assert report.check_days == [DAY]
    assert len(report.incidents) == 1

    html = render_html(report)
    assert "Daves&#39; Bakery" in html
    assert "0 of 1" in html
    assert "Tideline checked your website once this month, on 10 September." in html
    assert "fixed after 10 min" in html
    assert "is down: HTTP 503" in html

    text = render_text(report)
    assert "Up at: 0 of 1 check\n" in text
    assert "It was down at that check; what happened is below." in text
    assert "What happened:" in text
    assert "fixed after 10 min" in text


async def test_report_survives_the_raw_results_being_purged(sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
        await purge_old_results(session, DAY_START + timedelta(days=401))

    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 9)
    assert report.checks_run == 25
    assert report.uptime_percent == pytest.approx(91.667, abs=0.01)


async def test_a_quiet_month_says_so(sessionmaker, site_with_a_day):
    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 8)
    assert report.incidents == []
    assert report.uptime_text == "no checks"
    html = render_html(report)
    assert "Tideline has not checked this site yet this month." in html
    # Nothing happened, so there is no "What happened" section at all.
    assert "What happened" not in html


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
    assert "0 of 1" in page.text


# --- sending a whole month -------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 10, 1), (2026, 9)),
        (date(2026, 10, 15), (2026, 9)),
        (date(2027, 1, 1), (2026, 12)),  # across a year boundary
        (date(2026, 3, 1), (2026, 2)),
    ],
)
def test_previous_month(today, expected):
    from tideline.reports.monthly import previous_month

    assert previous_month(today) == expected


async def test_send_month_sends_one_report_per_active_site(sessionmaker, site_with_a_day):
    from tests.integration.test_runner_incidents import RecordingNotifier
    from tideline.reports.monthly import send_month

    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
        session.add(
            Site(name="Retired", domain="retired.ca", urls=["https://retired.ca/"], active=False)
        )

    notifier = RecordingNotifier()
    sent = await send_month(sessionmaker, notifier, 2026, 9)
    assert sent == 1  # the inactive site gets no report
    assert notifier.reports == ["September 2026 report: Daves' Bakery"]


async def test_one_failed_report_does_not_stop_the_rest(sessionmaker, site_with_a_day):
    from tests.integration.test_runner_incidents import RecordingNotifier
    from tideline.reports.monthly import send_month

    async with sessionmaker() as session, session.begin():
        second = Site(name="Second", domain="second.ca", urls=["https://second.ca/"])
        session.add(second)
        await session.flush()
        check = Check(site_id=second.id, kind="uptime", key="u2", interval_seconds=300, config={})
        session.add(check)
        await session.flush()
        session.add(
            CheckResult(
                check_id=check.id, started_at=DAY_START, duration_ms=1, status="ok", detail={}
            )
        )
        await session.flush()
        await rollup_day(session, DAY)

    notifier = RecordingNotifier()
    notifier.fail_next = 1
    sent = await send_month(sessionmaker, notifier, 2026, 9)
    assert sent == 1
    assert len(notifier.reports) == 1


async def test_site_page_shows_the_recent_checks(client, sessionmaker, site_with_a_day):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)

    page = await client.get(f"/sites/{site_with_a_day['site']}/view", auth=DASH)
    assert page.status_code == 200
    assert "Last 12 checks" in page.text
    # Two of that day's uptime checks failed, so it does not count as an up run.
    assert "Up at 0 of 1 check" in page.text
    assert "10 min" in page.text  # the resolved incident's duration
    assert "watched since 10 September" in page.text


async def test_site_page_without_rollups_says_so(client, site_with_a_day):
    page = await client.get(f"/sites/{site_with_a_day['site']}/view", auth=DASH)
    # Results exist but no run has been summarised yet: every cell is hollow.
    assert page.status_code == 200
    assert "No checks yet" in page.text
    assert "Hollow cells were before Tideline was watching." in page.text


async def test_reports_page_links_each_month(client, site_with_a_day, sessionmaker):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
    page = await client.get("/reports", auth=DASH)
    assert page.status_code == 200
    assert "September 2026" in page.text
    assert f'href="/reports/{site_with_a_day["site"]}/2026-09"' in page.text
    assert "up 0/1" in page.text


async def test_a_report_downloads_as_a_named_file(client, site_with_a_day, sessionmaker):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
    site_id = site_with_a_day["site"]
    page = await client.get(f"/reports/{site_id}/2026-09", auth=DASH)
    assert "content-disposition" not in page.headers
    assert "@media print" in page.text  # saving it as a PDF gives a clean page

    download = await client.get(f"/reports/{site_id}/2026-09?download=1", auth=DASH)
    assert download.headers["content-disposition"] == (
        'attachment; filename="daves-bakery-2026-09-report.html"'
    )


async def test_reports_open_in_the_dialog(client, site_with_a_day, sessionmaker):
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
    page = await client.get("/reports", auth=DASH)
    assert 'data-report="Daves&#39; Bakery, September 2026"' in page.text
    assert 'id="report-dialog"' in page.text
    assert "Next report" in page.text


async def test_a_narrowed_rerun_is_not_a_cell_on_the_strip(client, sessionmaker, site_with_a_day):
    from tideline.api.views import strips

    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
        # A day with only a TLS result: a re-run of one kind of check.
        tls = await session.scalar(select(Check).where(Check.kind == "tls"))
        session.add(
            CheckResult(
                check_id=tls.id,
                started_at=DAY_START + timedelta(days=2),
                duration_ms=1,
                status="ok",
                detail={},
            )
        )
        await session.flush()
        await rollup_day(session, DAY + timedelta(days=2))
    async with sessionmaker() as session:
        cells = (await strips(session, [site_with_a_day["site"]]))[site_with_a_day["site"]]
    assert [c.day for c in cells if c.day is not None] == [DAY]


async def test_an_open_incident_outranks_a_passing_result(sessionmaker, site_with_a_day):
    """A DNS change waiting to be accepted must not read "fine" (found by review)."""
    from tideline.api.queries import site_statuses

    async with sessionmaker() as session, session.begin():
        tls = await session.scalar(select(Check).where(Check.kind == "tls"))
        session.add(
            Incident(
                check_id=tls.id,
                opened_at=DAY_START,
                severity="critical",
                summary="Waiting for Owen",
            )
        )
    async with sessionmaker() as session:
        [site] = await site_statuses(session)
    [card] = [c for c in site.checks if c.kind == "tls"]
    assert (card.status, card.summary) == ("fail", "Waiting for Owen")
    assert site.status == "fail"


async def test_a_report_describes_its_own_month(sessionmaker, site_with_a_day):
    """Evidence from the month's last result, not today's (found by review)."""
    async with sessionmaker() as session, session.begin():
        await rollup_day(session, DAY)
        tls = await session.scalar(select(Check).where(Check.kind == "tls"))
        session.add(
            CheckResult(
                check_id=tls.id,
                started_at=datetime(2026, 10, 2, tzinfo=UTC),
                duration_ms=1,
                status="fail",
                detail={"summary": "Certificate expired"},
            )
        )
    async with sessionmaker() as session:
        report = await build_report(session, site_with_a_day["site"], 2026, 9)
    tls_line = next(w for w in report.watched if "certificate" in w.text)
    assert tls_line.tone == "up"  # in September it was fine
