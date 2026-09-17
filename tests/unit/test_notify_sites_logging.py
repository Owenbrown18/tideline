import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sitewatch.checks import DEFAULT_INTERVALS
from sitewatch.notify.base import (
    AlertMessage,
    LogNotifier,
    format_duration,
    render_body,
    render_subject,
)
from sitewatch.observability.logging import JsonFormatter, log_event
from sitewatch.sites import SiteEntry, SitesFile, check_specs, load_sites_file
from tests.conftest import FIXED_NOW

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
    assert render_subject(msg) == "[Sitewatch] PROBLEM: Daves' Bakery (davesbakery.ca) uptime"
    assert "HTTP 503" in render_body(msg)
    assert "Duration" not in render_body(msg)


def test_resolved_alert_includes_duration():
    msg = message(kind="resolved", resolved_at=FIXED_NOW + timedelta(minutes=47))
    assert render_subject(msg).startswith("[Sitewatch] RESOLVED:")
    assert "Duration: 47 min" in render_body(msg)


def test_alert_copy_has_no_em_dashes():
    for kind in ("open", "escalated", "reminder", "resolved"):
        msg = message(kind=kind, resolved_at=FIXED_NOW + timedelta(hours=1))
        assert "—" not in render_subject(msg) + render_body(msg)


async def test_log_notifier_writes_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="sitewatch.alerts")
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
