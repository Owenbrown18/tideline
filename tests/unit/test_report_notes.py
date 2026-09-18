"""The client-facing parts of the monthly report: "Coming up" and "What was watched"."""

from datetime import date

from tideline.api.queries import CheckStatus
from tideline.reports.monthly import _coming_up, _evidence, client_words

TODAY = date(2026, 9, 17)


def status(kind: str, state: str | None = "ok", **detail) -> CheckStatus:
    return CheckStatus(
        check_id=1,
        kind=kind,
        key=kind,
        enabled=True,
        interval_seconds=300,
        status=state,
        summary=None,
        last_checked_at=None,
        duration_ms=None,
        detail=detail or None,
    )


def test_domain_renewal_within_90_days_is_mentioned():
    notes = _coming_up(
        {"domain": status("domain", expiry="2026-11-02", registrar="Tucows Domains Inc.")}, TODAY
    )
    assert notes == [
        "Your domain renews on 2 November 2026. It is registered with Tucows Domains Inc. "
        "If it lapses, the website and email stop working."
    ]


def test_a_distant_renewal_is_not():
    assert _coming_up({"domain": status("domain", expiry="2027-06-01")}, TODAY) == []


def test_a_certificate_close_to_expiry_is_mentioned_calmly():
    [note] = _coming_up({"tls": status("tls", days_left=9)}, TODAY)
    assert "renews by itself" in note
    assert _coming_up({"tls": status("tls", days_left=60)}, TODAY) == []


def test_client_words_are_plain():
    assert client_words("form", "up") == "The contact form can deliver"
    assert client_words("form", "down") == "The contact form is not delivering"
    assert client_words("email_auth", "warn").startswith("Email from your domain")
    assert client_words("brand_new", "up") == "Brand new"


def test_evidence():
    assert _evidence(status("uptime")) == "every 5 min"
    assert _evidence(status("uptime", "fail")) == "failing"
    assert _evidence(status("domain", expiry="2027-03-04")) == "until 4 March 2027"
    assert _evidence(status("tls", not_after="2026-12-01T00:00:00+00:00")) == "until 1 December"
    assert _evidence(status("links", links_checked=42)) == "42 links"
    assert _evidence(status("form", None)) == "not checked yet"
