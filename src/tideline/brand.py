"""Tideline's brand, as code: names, status words, the voice, and the favicon.

Everything a person reads (the dashboard, alert emails, monthly reports) takes
its wording from here, so the product speaks with one voice. The rules come
from the brand proposal (docs/brand.md):

- Calm and specific. One sentence that says how things are, then the facts.
- No exclamation marks. Name the site, say what is wrong, say what to do.
- Status is always a shape and a word as well as a colour.

Sitewatch was the working name. The code says Tideline; a few server and AWS
names still say sitewatch (see docs/brand.md, "The rename").
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

PRODUCT = "Tideline"
ENDORSEMENT = "by OBdesign"
TAGLINE = "Quiet until it matters."

Tone = Literal["up", "warn", "down", "none"]

# Result status (ok/warn/fail, or None before the first result) to the tone the
# interface draws: its shape, colour and word.
TONE_OF: dict[str | None, Tone] = {"ok": "up", "warn": "warn", "fail": "down", None: "none"}
WORD_OF: dict[Tone, str] = {"up": "Up", "warn": "Warning", "down": "Down", "none": "No data"}

CHECK_NAMES: dict[str, str] = {
    "uptime": "Uptime",
    "content": "Page content",
    "tls": "Certificate",
    "domain": "Domain",
    "dns": "DNS",
    "email_auth": "Email authentication",
    "links": "Links",
    "form": "Contact form",
}

# The order checks appear in on a site page: what a visitor would notice first.
CHECK_ORDER = ["uptime", "content", "form", "links", "tls", "domain", "dns", "email_auth"]

# What Owen should do about a failing check. Plain instructions, one or two
# sentences, written for the moment an alert arrives.
GUIDANCE: dict[str, str] = {
    "uptime": "Open the site yourself. If it is down for you too, check the latest "
    "deploy and the hosting provider's status page.",
    "content": "Look at the page. A missing business name usually means a bad deploy; "
    "a spam marker means the site may have been compromised.",
    "tls": "Certificates on Vercel renew by themselves, so one this close to expiry "
    "means renewal is failing. Check the domain's settings in Vercel.",
    "domain": "Renew the domain at its registrar before it lapses. If it lapses, the "
    "website and its email stop working.",
    "dns": "If the change was intended, accept it as the new baseline. If not, "
    "someone has changed the domain's records.",
    "email_auth": "Add the missing record at the domain's DNS host. The exact records "
    "for each domain are in the vault note on client email authentication.",
    "links": "Fix or remove the broken link on the page it was found on.",
    "form": "Send one enquiry through the form yourself. Tideline never submits a "
    "client's form, because that would email them.",
}

# The headline when a check fails: what a visitor would notice, not the check's name.
FAILING: dict[str, str] = {
    "uptime": "{site} is down",
    "content": "The page isn't showing what it should",
    "form": "The contact form isn't delivering",
    "links": "A link on the site is broken",
    "tls": "The certificate needs attention",
    "domain": "The domain needs renewing",
    "dns": "DNS has changed",
    "email_auth": "Email authentication is broken",
}

NUMBER_WORDS = [
    "No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve",
]  # fmt: skip


def human_date(when: "date | datetime", year: bool = True) -> str:
    """14 December 2026, the way a person writes a date (no leading zero)."""
    text = f"{when.day} {when.strftime('%B')}"
    return f"{text} {when.year}" if year else text


def short_url(url: str, host: str | None = None) -> str:
    """A URL as a person would say it: the path on the site's own host,
    host plus path elsewhere, and never the tracking query string."""
    parts = urlparse(url)
    path = parts.path.rstrip("/") or "/"
    if host and parts.netloc.lower().removeprefix("www.") == host.lower().removeprefix("www."):
        return path
    return f"{parts.netloc.removeprefix('www.')}{'' if path == '/' else path}"


def percent(value: float | None, empty: str = "\u2013") -> str:
    """Uptime as people read it: "99.95%", "100%", never "99.954%".

    Rounds down, so a month with any downtime can never display as 100%.
    """
    if value is None:
        return empty
    floored = math.floor(value * 100 + 1e-9) / 100
    return f"{floored:.2f}".rstrip("0").rstrip(".") + "%"


def local(when: datetime, fmt: str, zone: str = "America/Vancouver") -> str:
    """Format a stored (UTC) time in the zone Owen reads it in."""
    return when.astimezone(ZoneInfo(zone)).strftime(fmt)


def number_word(n: int) -> str:
    return NUMBER_WORDS[n] if 0 <= n < len(NUMBER_WORDS) else str(n)


def check_name(kind: str) -> str:
    return CHECK_NAMES.get(kind, kind.replace("_", " ").capitalize())


def check_noun(kind: str) -> str:
    """The check's name inside a sentence: "contact form", but still "DNS"."""
    name = check_name(kind)
    return name if name.isupper() else name.lower()


def failing_headline(kind: str, site: str) -> str:
    """One sentence for a failing check: "The contact form isn't delivering"."""
    return FAILING.get(kind, f"{check_name(kind)} is failing").format(site=site)


def tone(status: str | None) -> Tone:
    return TONE_OF.get(status, "none")


def worst(tones: list[Tone]) -> Tone:
    """The tone a group of things reads as: the worst of them."""
    order: tuple[Tone, ...] = ("down", "warn", "up")
    for candidate in order:
        if candidate in tones:
            return candidate
    return "none"


@dataclass(frozen=True)
class Headline:
    """The one sentence at the top of the dashboard, and the line under it."""

    text: str
    tone: Tone
    detail: str


def headline(
    total: int,
    down_names: list[str],
    warning_count: int,
    waiting: bool = False,
) -> Headline:
    """How things are, in one sentence.

    >>> headline(11, [], 0).text
    'All 11 sites are up'
    >>> headline(11, ["SOMA Active Health", "Figs & Honey"], 8).text
    'Two sites need you'
    """
    if waiting or total == 0:
        return Headline(
            "Waiting for the first checks", "none", "Every site is checked on the 1st and 15th."
        )
    if down_names:
        n = len(down_names)
        subject = "One site needs you" if n == 1 else f"{number_word(n)} sites need you"
        up_count = total - n
        rest = f" The other {up_count} {'is' if up_count == 1 else 'are'} up." if up_count else ""
        return Headline(subject, "down", f"{_join(down_names)}.{rest}")
    everyone = "The site is up" if total == 1 else f"All {total} sites are up"
    if warning_count:
        things = "warning" if warning_count == 1 else "warnings"
        return Headline(
            everyone,
            "warn",
            f"{number_word(warning_count)} {things} to look at when you have time.",
        )
    return Headline(everyone, "up", "Nothing needs you.")


def _join(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# --- the favicon: the T. mark, whose period is the status light ----------------

# Colours on the Deep background (brand proposal, "On deep").
_FAVICON_FILL: dict[Tone, str] = {
    "up": "#86b0d6",
    "warn": "#e0a93f",
    "down": "#e8705a",
    "none": "#6f7a86",
}


def favicon_svg(state: Tone) -> str:
    """A 64x64 SVG of the mark with the period drawn in the status shape.

    The T is drawn as paths rather than text, because a favicon cannot load a
    web font: this keeps the mark identical in every browser.
    """
    fill = _FAVICON_FILL[state]
    if state == "warn":
        period = f'<path d="M50 38 L57.5 51 H42.5 Z" fill="{fill}"/>'
    elif state == "down":
        period = f'<rect x="43.5" y="39.5" width="12" height="12" rx="1.5" fill="{fill}"/>'
    elif state == "none":
        period = (
            f'<circle cx="49.5" cy="45.5" r="5" fill="none" stroke="{fill}" stroke-width="2.4"/>'
        )
    else:
        period = f'<circle cx="49.5" cy="45.5" r="6" fill="{fill}"/>'
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<defs><radialGradient id="g" cx="26%" cy="20%" r="90%">'
        '<stop offset="0" stop-color="#2c4a6e"/><stop offset=".4" stop-color="#16304c"/>'
        '<stop offset=".8" stop-color="#0b1826"/></radialGradient></defs>'
        '<rect width="64" height="64" rx="14" fill="url(#g)"/>'
        # A serif T: the bar with bracketed ends, and the stem with a foot.
        '<path fill="#f7f6f3" d="M9 12 H45 V22 H42 C41 17 39 16 34 16 H31 V47 '
        "C31 49 32 50 35 50 V52 H19 V50 C22 50 23 49 23 47 V16 H20 C15 16 13 17 12 22 "
        'H9 Z"/>'
        f"{period}</svg>"
    )
