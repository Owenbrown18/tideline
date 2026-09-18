"""One email per run, instead of one per incident.

Tideline runs twice a month. Sending an email for every incident a run opens
or closes would bunch several emails into the same minute, so the runner hands
its alerts to a `Digest` instead. The digest collects them, and at the end of
the run sends Owen one summary, and only when something changed: a new problem,
one that got worse, or one that got fixed. Problems that were already open are
listed in that email, but on their own they do not send one. A quiet run sends
nothing, and the monthly report still arrives on the 1st.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from tideline import brand
from tideline.config import get_settings
from tideline.notify.base import AlertMessage, AlertView, Notifier, describe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Alert kinds that make a run worth an email.
NEWS = ("open", "escalated", "resolved")

TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class StillOpen:
    """An incident that was open before this run and still is."""

    site_name: str
    check_kind: str
    severity: str
    summary: str
    opened_at: datetime


@dataclass
class Digest:
    """A notifier that collects a run's alerts, then sends them as one email.

    Alerts are recorded (an `alerts` row, the incident's `last_alerted_at`) only
    after that email has gone out: see `record`. Until then an unsent "open"
    alert is retried on the next run, like any alert that failed to send.
    """

    inner: Notifier
    messages: list[AlertMessage] = field(default_factory=list)
    records_later = True

    @property
    def channel(self) -> str:
        # Recorded on each alert row, so the dashboard can say "emailed" or "logged".
        return self.inner.channel

    async def send(self, msg: AlertMessage) -> None:
        self.messages.append(msg)

    async def send_report(self, subject: str, text: str, html: str) -> str:
        return await self.inner.send_report(subject, text, html)

    def has_news(self) -> bool:
        return any(m.kind in NEWS for m in self.messages)

    def incident_ids(self) -> set[int]:
        return {m.incident_id for m in self.messages if m.incident_id is not None}

    async def record(self, session: "AsyncSession", when: datetime) -> int:
        """Write down the alerts this digest delivered. Returns how many."""
        from tideline.db.models import Alert, Incident

        recorded = 0
        for message in self.messages:
            if message.incident_id is None:
                continue
            incident = await session.get(Incident, message.incident_id)
            if incident is None:
                continue
            incident.last_alerted_at = when
            session.add(
                Alert(
                    incident_id=incident.id, channel=self.channel, sent_at=when, kind=message.kind
                )
            )
            recorded += 1
        return recorded

    async def flush(self, when: datetime, still_open: list[StillOpen]) -> str | None:
        """Send the run's summary if anything changed. Returns the message id, or None."""
        if not self.has_news():
            return None
        summary = RunSummary.of(self.messages, still_open, when)
        return await self.inner.send_report(
            summary.subject, render_text(summary), render_html(summary)
        )


@dataclass(frozen=True)
class Item:
    view: AlertView
    site_name: str


@dataclass(frozen=True)
class RunSummary:
    subject: str
    headline: str
    tone: brand.Tone
    when: datetime
    new: list[Item]
    fixed: list[Item]
    still_open: list[StillOpen]
    link: str | None

    @classmethod
    def of(
        cls, messages: list[AlertMessage], still_open: list[StillOpen], when: datetime
    ) -> "RunSummary":
        new = [Item(describe(m), m.site_name) for m in messages if m.kind in ("open", "escalated")]
        fixed = [Item(describe(m), m.site_name) for m in messages if m.kind == "resolved"]
        critical = [i for i in new if i.view.tone == "down"] + [
            s for s in still_open if s.severity == "critical"
        ]
        tone: brand.Tone = "down" if critical else ("warn" if new or still_open else "up")

        parts = []
        if new:
            parts.append(f"{len(new)} new {'problem' if len(new) == 1 else 'problems'}")
        if fixed:
            parts.append(f"{len(fixed)} fixed")
        subject = "Tideline check: " + ", ".join(parts)

        if new:
            count = brand.number_word(len(new))
            headline = (
                f"{count} new {'problem' if len(new) == 1 else 'problems'} since the last check."
            )
        else:
            count = brand.number_word(len(fixed))
            noun = "problem" if len(fixed) == 1 else "problems"
            headline = f"{count} {noun} fixed since the last check."
        base = get_settings().public_url
        return cls(
            subject=subject,
            headline=headline,
            tone=tone,
            when=when,
            new=new,
            fixed=fixed,
            still_open=still_open,
            link=base.rstrip("/") + "/" if base else None,
        )


def render_text(s: RunSummary) -> str:
    zone = get_settings().display_timezone
    lines = [s.headline, f"Checked {brand.local(s.when, '%A %-d %B, %H:%M %Z', zone)}.", ""]
    if s.new:
        lines.append("New:")
        for item in s.new:
            lines.append(f"  x {item.view.headline} {item.view.summary}")
            if item.view.guidance:
                lines.append(f"    What to do: {item.view.guidance}")
        lines.append("")
    if s.fixed:
        lines.append("Fixed:")
        lines += [f"  o {i.view.headline}" for i in s.fixed]
        lines.append("")
    if s.still_open:
        lines.append("Still open:")
        lines += [
            f"  ! {o.site_name}, {brand.check_noun(o.check_kind)}: {o.summary}"
            for o in s.still_open
        ]
        lines.append("")
    if s.link:
        lines.append(f"Open in Tideline: {s.link}")
    lines.append("Tideline checks every site on the 1st and 15th. Alerts go to Owen only.")
    return "\n".join(lines)


def render_html(s: RunSummary) -> str:
    template = TEMPLATES.get_template("digest.html")
    zone = get_settings().display_timezone
    return template.render(
        s=s, when=brand.local(s.when, "%A %-d %B, %H:%M %Z", zone), check_noun=brand.check_noun
    )
