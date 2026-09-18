"""Tideline's wording and favicon (src/tideline/brand.py)."""

from datetime import UTC, date, datetime

import pytest

from tideline import brand


@pytest.mark.parametrize(
    ("args", "text", "tone", "detail"),
    [
        ((11, [], 0), "All 11 sites are up", "up", "Nothing needs you."),
        ((1, [], 0), "The site is up", "up", "Nothing needs you."),
        ((11, [], 1), "All 11 sites are up", "warn", "One warning to look at when you have time."),
        (
            (11, [], 8),
            "All 11 sites are up",
            "warn",
            "Eight warnings to look at when you have time.",
        ),
        ((11, ["SOMA"], 0), "One site needs you", "down", "SOMA. The other 10 are up."),
        ((2, ["A", "B"], 0), "Two sites need you", "down", "A and B."),
        ((3, ["A", "B"], 0), "Two sites need you", "down", "A and B. The other 1 is up."),
        (
            (5, ["A", "B", "C"], 0),
            "Three sites need you",
            "down",
            "A, B and C. The other 2 are up.",
        ),
        ((0, [], 0), "Waiting for the first checks", "none", "Results appear within a minute."),
    ],
)
def test_headline(args, text, tone, detail):
    head = brand.headline(*args)
    assert (head.text, head.tone, head.detail) == (text, tone, detail)


def test_a_site_that_is_down_outranks_warnings():
    assert brand.headline(11, ["SOMA"], 8).tone == "down"


def test_waiting_wins_even_with_sites():
    assert brand.headline(11, [], 0, waiting=True).tone == "none"


@pytest.mark.parametrize(
    ("url", "host", "short"),
    [
        ("https://soma.ca/api/contact", "soma.ca", "/api/contact"),
        ("https://www.soma.ca/testimonials/", "soma.ca", "/testimonials"),
        ("https://soma.ca/", "soma.ca", "/"),
        ("https://formspree.io/f/abc?utm_source=x", "soma.ca", "formspree.io/f/abc"),
        ("https://www.partner.test/", None, "partner.test"),
    ],
)
def test_short_url(url, host, short):
    assert brand.short_url(url, host) == short


def test_human_date_has_no_leading_zero():
    assert brand.human_date(date(2026, 12, 4)) == "4 December 2026"
    assert brand.human_date(date(2026, 12, 4), year=False) == "4 December"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (100.0, "100%"),
        (99.999, "99.99%"),  # never rounds up to a false 100%
        (99.95, "99.95%"),
        (91.667, "91.66%"),
        (60.0, "60%"),
        (99.5, "99.5%"),
        (None, "\u2013"),
    ],
)
def test_percent(value, text):
    assert brand.percent(value) == text


def test_local_time_is_pacific():
    noon_utc = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    assert brand.local(noon_utc, "%H:%M %Z") == "05:00 PDT"
    # British Columbia stays on UTC-7 all year from November 2026, which the
    # time zone database labels MST. Using the zone, not a fixed offset, gets it right.
    assert brand.local(datetime(2026, 12, 17, 12, 0, tzinfo=UTC), "%H:%M %Z") == "05:00 MST"


def test_number_words_stop_at_twelve():
    assert brand.number_word(0) == "No"
    assert brand.number_word(12) == "Twelve"
    assert brand.number_word(13) == "13"


def test_check_names():
    assert brand.check_name("email_auth") == "Email authentication"
    assert brand.check_name("new_thing") == "New thing"  # an unknown kind still reads well
    assert brand.check_noun("form") == "contact form"
    assert brand.check_noun("dns") == "DNS"
    assert brand.failing_headline("uptime", "SOMA") == "SOMA is down"
    assert brand.failing_headline("form", "SOMA") == "The contact form isn't delivering"


def test_every_check_has_a_name_guidance_and_a_headline():
    for kind in brand.CHECK_ORDER:
        assert kind in brand.CHECK_NAMES
        assert kind in brand.GUIDANCE
        assert kind in brand.FAILING


def test_tone_and_worst():
    assert [brand.tone(s) for s in ("ok", "warn", "fail", None)] == ["up", "warn", "down", "none"]
    assert brand.worst(["up", "warn", "none"]) == "warn"
    assert brand.worst(["up", "down"]) == "down"
    assert brand.worst([]) == "none"
    assert brand.worst(["none"]) == "none"


@pytest.mark.parametrize(
    ("state", "shape"),
    [
        ("up", '<circle cx="49.5" cy="45.5" r="6" fill="#86b0d6"/>'),
        ("warn", '<path d="M50 38 L57.5 51 H42.5 Z" fill="#e0a93f"/>'),
        ("down", 'fill="#e8705a"/>'),
        ("none", 'fill="none" stroke="#6f7a86"'),
    ],
)
def test_favicon_period_is_the_status_shape(state, shape):
    svg = brand.favicon_svg(state)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">')
    assert shape in svg


def test_nothing_written_uses_em_dashes():
    words = [*brand.GUIDANCE.values(), *brand.FAILING.values(), brand.TAGLINE]
    assert not any("—" in w for w in words)
