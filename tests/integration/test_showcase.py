"""The showcase database: six months of the real product against invented sites."""

import sqlite3
from datetime import UTC, datetime

import pytest

from tideline.showcase import SITES, build, run_times

NOW = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)


def test_run_times_are_the_1st_and_15th_at_7am_pacific():
    times = run_times(NOW)
    assert len(times) == 12
    assert times[-1] == datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    assert times[0] == datetime(2026, 4, 1, 14, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def showcase(tmp_path_factory) -> tuple[sqlite3.Connection, list[str]]:
    out = tmp_path_factory.mktemp("showcase") / "showcase.db"
    emails = build(out, now=NOW, pace=0)
    return sqlite3.connect(out), emails


def test_it_holds_every_invented_site_and_twelve_runs(showcase):
    db, _ = showcase
    assert db.execute("select count(*) from sites").fetchone()[0] == len(SITES)
    days = db.execute("select count(distinct day) from daily_rollups").fetchone()[0]
    assert days == 12


def test_the_incidents_follow_the_script(showcase):
    db, _ = showcase
    rows = db.execute(
        "select s.name, c.kind, i.severity, i.resolved_at is null "
        "from incidents i join checks c on c.id = i.check_id join sites s on s.id = c.site_id "
        "order by i.opened_at"
    ).fetchall()
    assert rows == [
        ("Saltspring Pottery Studio", "email_auth", "warning", 1),
        ("Harbour & Hearth Bakery", "uptime", "critical", 0),  # down one run, then back
        ("Lantern Cycle Works", "dns", "critical", 0),  # a DNS change, accepted
        ("Cedar & Stone Landscaping", "links", "warning", 0),  # a broken link, fixed
        ("Ridgeback Roofing", "uptime", "critical", 1),  # down now
        ("Northwind Dental", "form", "critical", 1),  # form broken now
    ]


def test_one_outage_is_one_incident(showcase):
    db, _ = showcase
    # Harbour & Hearth was fully down for a run: uptime reports it, and the
    # content, links and form checks stay quiet rather than piling on.
    kinds = db.execute(
        "select c.kind from incidents i join checks c on c.id = i.check_id "
        "join sites s on s.id = c.site_id where s.name = 'Harbour & Hearth Bakery'"
    ).fetchall()
    assert kinds == [("uptime",)]


def test_reports_are_only_for_months_the_site_was_checked(showcase):
    _, emails = showcase
    reports = [e for e in emails if "report:" in e]
    assert not any(e.startswith("March 2026") for e in reports)  # before the first run
    assert sum(e.startswith("April 2026") for e in reports) == len(SITES)
