"""Alert messages and the notifier interface.

Every alert goes to Owen only. Sitewatch has no client email addresses at all,
so it cannot email a client even by mistake: that is a design rule, not a setting.

M1 ships `LogNotifier`, which writes the alert as a log line. M4 adds SES email
behind the same interface, so the incident code never changes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sitewatch.observability.logging import log_event

AlertKind = Literal["open", "escalated", "reminder", "resolved"]

log = logging.getLogger("sitewatch.alerts")


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


def render_subject(msg: AlertMessage) -> str:
    label = {
        "open": "PROBLEM",
        "escalated": "NOW CRITICAL",
        "reminder": "STILL OPEN",
        "resolved": "RESOLVED",
    }[msg.kind]
    return f"[Sitewatch] {label}: {msg.site_name} ({msg.domain}) {msg.check_kind}"


def render_body(msg: AlertMessage) -> str:
    lines = [
        f"Site: {msg.site_name} ({msg.domain})",
        f"Check: {msg.check_kind} ({msg.check_key})",
        f"Severity: {msg.severity}",
        f"Opened: {msg.opened_at:%Y-%m-%d %H:%M} UTC",
    ]
    if msg.duration is not None and msg.resolved_at is not None:
        lines.append(f"Resolved: {msg.resolved_at:%Y-%m-%d %H:%M} UTC")
        lines.append(f"Duration: {format_duration(msg.duration)}")
    lines += ["", msg.summary]
    return "\n".join(lines)


class Notifier(Protocol):
    channel: str

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
