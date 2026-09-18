import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import FIXED_NOW
from tideline.checks import DEFAULT_INTERVALS
from tideline.notify.base import (
    AlertMessage,
    LogNotifier,
    describe,
    format_duration,
    render_body,
    render_html,
    render_subject,
)
from tideline.observability.logging import JsonFormatter, log_event
from tideline.sites import SiteEntry, SitesFile, check_specs, load_sites_file

REPO = Path(__file__).resolve().parents[2]


# --- alert messages ----------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=20), "0 min"),
        (timedelta(minutes=12), "12 min"),
        (timedelta(hours=2), "2 h"),
        (timedelta(hours=26, minutes=5), "1 d 2 h 5 min"),
    ],
)
def test_format_duration(delta, text):
    assert format_duration(delta) == text


def message(**overrides):
    values = dict(
        kind="open",
        site_name="Daves' Bakery",
        domain="davesbakery.ca",
        check_kind="uptime",
        check_key="uptime:https://davesbakery.ca/",
        severity="critical",
        summary="https://davesbakery.ca/ is down: HTTP 503",
        opened_at=FIXED_NOW,
    )
    return AlertMessage(**(values | overrides))


def test_open_alert_text():
    msg = message()
    assert render_subject(msg) == "Down: Daves' Bakery"
    body = render_body(msg)
    assert body.startswith("Daves' Bakery is down.")
    assert "HTTP 503" in body
    assert "What to do:" in body
    assert "Lasted" not in body


@pytest.mark.parametrize(
    ("overrides", "subject"),
    [
        ({"check_kind": "form"}, "Down: Daves' Bakery, contact form"),
        ({"kind": "escalated", "check_kind": "tls"}, "Now critical: Daves' Bakery, certificate"),
        ({"kind": "reminder"}, "Still down: Daves' Bakery"),
        (
            {"severity": "warning", "check_kind": "email_auth"},
            "Warning: Daves' Bakery, email authentication",
        ),
        (
            {"severity": "warning", "kind": "reminder", "check_kind": "dns"},
            "Still a warning: Daves' Bakery, DNS",
        ),
    ],
)
def test_alert_subjects_name_the_site_and_the_check(overrides, subject):
    assert render_subject(message(**overrides)) == subject


def test_resolved_alert_says_when_it_was_found_and_fixed_by():
    msg = message(kind="resolved", resolved_at=FIXED_NOW + timedelta(days=14))
    # Not "after 14 days": two checks a month cannot know how long it lasted.
    assert render_subject(msg) == "Fixed: Daves' Bakery"
    body = render_body(msg)
    assert body.startswith("Daves' Bakery is back up.")
    assert "It was: https://davesbakery.ca/ is down: HTTP 503." in body
    facts = dict(describe(msg, zone="America/Vancouver").facts)
    assert facts["Found"].startswith("Thursday 17 September")
    assert facts["Fixed by"].startswith("Thursday 1 October")
    assert "Lasted" not in body
    # Nothing to do once it is fixed.
    assert "What to do" not in body


def test_alert_times_are_in_pacific_time():
    # FIXED_NOW is 12:00 UTC, which is 05:00 in Vancouver in September (PDT).
    facts = dict(describe(message(), zone="America/Vancouver").facts)
    assert facts["Found"].endswith("05:00 PDT")


def test_alert_links_to_the_site_page_only_when_the_address_is_known():
    msg = message(site_id=7)
    assert describe(msg, base_url="https://status.example.ca/").link == (
        "https://status.example.ca/sites/7/view"
    )
    assert describe(msg, base_url="").link is None
    assert describe(message(), base_url="https://status.example.ca").link is None


def test_alert_html_matches_the_text():
    msg = message(check_kind="form", summary="The form posts to /api/contact, which returns 404")
    html = render_html(msg)
    assert "Daves&#39; Bakery&#39;s contact form isn&#39;t delivering." in html
    assert "/api/contact" in html
    assert "#c0432f" in html  # the "down" stripe
    warning = render_html(message(severity="warning", check_kind="email_auth"))
    assert "#a8740f" in warning


def test_alert_copy_has_no_em_dashes():
    for kind in ("open", "escalated", "reminder", "resolved"):
        msg = message(kind=kind, resolved_at=FIXED_NOW + timedelta(hours=1))
        text = render_subject(msg) + render_body(msg) + render_html(msg)
        assert "\u2014" not in text


async def test_log_notifier_writes_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="tideline.alerts")
    await LogNotifier().send(message())
    [record] = caplog.records
    assert record.getMessage() == "alert"
    assert record.alert_kind == "open"
    assert record.site == "davesbakery.ca"


# --- JSON logging -------------------------------------------------------------


def test_json_formatter_includes_structured_fields():
    logger = logging.getLogger("test.json")
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_event(logger, "check_result", site="example.ca", status="ok", duration_ms=12)
    finally:
        logger.removeHandler(handler)
    line = json.loads(JsonFormatter().format(records[0]))
    assert line["event"] == "check_result"
    assert line["site"] == "example.ca"
    assert line["duration_ms"] == 12
    assert line["level"] == "INFO"
    assert line["ts"].endswith("+00:00")


# --- sites.yaml -----------------------------------------------------------------


def test_example_sites_file_is_valid():
    sites = load_sites_file(REPO / "sites.example.yaml")
    assert len(sites.sites) == 2


def test_demo_sites_file_is_valid():
    sites = load_sites_file(REPO / "demo" / "sites.demo.yaml")
    assert sites.sites


def test_missing_sites_file_explains_what_to_do(tmp_path):
    with pytest.raises(FileNotFoundError, match=r"sites\.example\.yaml"):
        load_sites_file(tmp_path / "sites.yaml")


def test_default_checks_for_a_site():
    specs = check_specs(SiteEntry(name="Example", domain="Example.CA"), {})
    assert [(s.kind, s.key) for s in specs] == [
        ("uptime", "uptime:https://example.ca/"),
        ("content", "content:https://example.ca/"),
        ("tls", "tls:example.ca"),
        ("domain", "domain:example.ca"),
        ("dns", "dns:example.ca"),
        ("links", "links:https://example.ca/"),
        ("form", "form:example.ca"),
        ("email_auth", "email_auth:example.ca"),
    ]
    assert {s.kind: s.interval_seconds for s in specs} == DEFAULT_INTERVALS
    assert all(s.enabled for s in specs)


def test_each_url_gets_uptime_and_content_checks():
    entry = SiteEntry(name="E", domain="e.ca", urls=["https://e.ca/", "https://e.ca/contact"])
    kinds = [s.kind for s in check_specs(entry, {})]
    assert kinds.count("uptime") == 2
    assert kinds.count("content") == 2


def test_interval_overrides_and_disabled_checks():
    entry = SiteEntry(
        name="E",
        domain="e.ca",
        intervals={"uptime": 60},
        checks={"domain": False},
        spam_ignore=["porn"],
    )
    specs = {s.kind: s for s in check_specs(entry, {"intervals": {"uptime": 120, "tls": 3600}})}
    assert specs["uptime"].interval_seconds == 60  # site beats defaults
    assert specs["tls"].interval_seconds == 3600  # file defaults beat README defaults
    assert specs["domain"].enabled is False
    assert specs["content"].config["spam_ignore"] == ["porn"]


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "E", "domain": "https://e.ca/"},
        {"name": "E", "domain": "e.ca", "checks": {"lighthouse": True}},
        {"name": "E", "domain": "e.ca", "typo_field": 1},
    ],
)
def test_invalid_site_entries_are_rejected(bad):
    with pytest.raises(ValidationError):
        SitesFile.model_validate({"sites": [bad]})
