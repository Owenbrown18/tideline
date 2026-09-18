"""Alert messages and the notifier interface.

Every alert goes to Owen only. Tideline has no client email addresses at all,
so it cannot email a client even by mistake: that is a design rule, not a setting.

M1 ships `LogNotifier`, which writes the alert as a log line. M4 adds SES email
behind the same interface, so the incident code never changes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tideline import brand
from tideline.config import get_settings
from tideline.observability.logging import log_event

AlertKind = Literal["open", "escalated", "reminder", "resolved"]

log = logging.getLogger("tideline.alerts")


@dataclass(frozen=True)
class AlertMessage:
    kind: AlertKind
    site_name: str
    domain: str
    check_kind: str
    check_key: str
    severity: str
    summary: str
    opened_at: datetime
    resolved_at: datetime | None = None
    # For the "Open in Tideline" link; None leaves the link out.
    site_id: int | None = None
    # The incident this alert is about, so it can be recorded once it has been sent.
    incident_id: int | None = None

    @property
    def duration(self) -> timedelta | None:
        return self.resolved_at - self.opened_at if self.resolved_at else None


def format_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{days} d"] if days else []
    if hours:
        parts.append(f"{hours} h")
    if mins or not parts:
        parts.append(f"{mins} min")
    return " ".join(parts)


@dataclass(frozen=True)
class AlertView:
    """Everything an alert says, worked out once for the subject, text and HTML."""

    subject: str
    headline: str
    tone: brand.Tone
    summary: str
    facts: list[tuple[str, str]]
    guidance: str | None
    link: str | None
    footer: str


def describe(msg: AlertMessage, zone: str = "", base_url: str | None = None) -> AlertView:
    """Turn an alert into words, in Tideline's voice (docs/brand.md)."""
    settings = get_settings()
    zone = zone or settings.display_timezone
    base_url = settings.public_url if base_url is None else base_url
    critical = msg.severity == "critical"
    noun = brand.check_noun(msg.check_kind)
    about = msg.site_name if msg.check_kind == "uptime" else f"{msg.site_name}, {noun}"

    if msg.kind == "resolved":
        label = "Fixed"
        tone: brand.Tone = "up"
        headline = (
            f"{msg.site_name} is back up."
            if msg.check_kind == "uptime"
            else f"{msg.site_name}'s {noun} is working again."
        )
    elif critical:
        label = {"open": "Down", "escalated": "Now critical", "reminder": "Still down"}[msg.kind]
        tone = "down"
        headline = (
            ALERT_HEADLINES.get(msg.check_kind, "{site}'s {noun} is failing").format(
                site=msg.site_name, noun=noun
            )
            + "."
        )
    else:
        label = "Still a warning" if msg.kind == "reminder" else "Warning"
        tone = "warn"
        headline = f"{msg.site_name}'s {noun} needs a look."

    subject = f"{label}: {about}"
    when = "%A %-d %B, %H:%M %Z"
    facts = [
        ("Site", msg.domain),
        ("Check", brand.check_name(msg.check_kind)),
        ("Since", brand.local(msg.opened_at, when, zone)),
    ]
    if msg.duration is not None and msg.resolved_at is not None:
        subject += f", after {format_duration(msg.duration)}"
        facts.append(("Fixed", brand.local(msg.resolved_at, when, zone)))
        facts.append(("Lasted", format_duration(msg.duration)))

    footer = {
        "open": "You'll get one more email when this is fixed.",
        "escalated": "This has been failing long enough to count as critical.",
        "reminder": "It is listed in every check's summary while it stays open.",
        "resolved": "Nothing more to do.",
    }[msg.kind] + " Alerts go to Owen only; Tideline never emails a client."

    return AlertView(
        subject=subject,
        headline=headline,
        tone=tone,
        summary=_sentence(
            f"It was: {_lower_first(msg.summary)}" if msg.kind == "resolved" else msg.summary
        ),
        facts=facts,
        guidance=None if msg.kind == "resolved" else brand.GUIDANCE.get(msg.check_kind),
        link=f"{base_url.rstrip('/')}/sites/{msg.site_id}/view"
        if base_url and msg.site_id is not None
        else None,
        footer=footer,
    )


def _sentence(text: str) -> str:
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _lower_first(text: str) -> str:
    """Lower-cases "The site is down" but leaves "HTTP 503" alone."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


# The headline of a critical alert: what a visitor would notice, about this site.
ALERT_HEADLINES: dict[str, str] = {
    "uptime": "{site} is down",
    "content": "{site}'s page isn't showing what it should",
    "form": "{site}'s contact form isn't delivering",
    "links": "A link on {site} is broken",
    "tls": "{site}'s certificate needs attention",
    "domain": "{site}'s domain needs renewing",
    "dns": "{site}'s DNS has changed",
    "email_auth": "{site}'s email authentication is broken",
}


def render_subject(msg: AlertMessage) -> str:
    return describe(msg).subject


def render_body(msg: AlertMessage) -> str:
    """The plain-text version: what every mail client can show."""
    view = describe(msg)
    width = max(len(label) for label, _ in view.facts)
    lines = [view.headline, "", view.summary, ""]
    lines += [f"{label.ljust(width)}  {value}" for label, value in view.facts]
    if view.guidance:
        lines += ["", f"What to do: {view.guidance}"]
    if view.link:
        lines += ["", f"Open in Tideline: {view.link}"]
    lines += ["", view.footer]
    return "\n".join(lines)


TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_html(msg: AlertMessage) -> str:
    """The HTML version, laid out like the alert in the brand proposal."""
    return TEMPLATES.get_template("alert.html").render(view=describe(msg))


class Notifier(Protocol):
    @property
    def channel(self) -> str:
        """Where messages go: "ses" (email) or "log" (local development)."""

    async def send(self, msg: AlertMessage) -> None:
        """Deliver the alert or raise. The caller records only alerts that were sent."""

    async def send_report(self, subject: str, text: str, html: str) -> str:
        """Deliver a monthly report to Owen. Returns an id for the log."""


class LogNotifier:
    channel = "log"

    async def send_report(self, subject: str, text: str, html: str) -> str:
        log_event(log, "report", logging.INFO, subject=subject, text=text)
        return "logged"

    async def send(self, msg: AlertMessage) -> None:
        log_event(
            log,
            "alert",
            logging.WARNING,
            alert_kind=msg.kind,
            site=msg.domain,
            check_kind=msg.check_kind,
            check_key=msg.check_key,
            severity=msg.severity,
            subject=render_subject(msg),
            summary=msg.summary,
            duration=format_duration(msg.duration) if msg.duration else None,
        )
