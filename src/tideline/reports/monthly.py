"""The monthly report for one site.

Built from the daily rollups (uptime, response times) plus the incident records
for the month, so it works even after the raw results have been purged.

It goes to Owen, never to a client: Tideline holds no client addresses. Owen
decides what to forward, and the HTML is written so it can be forwarded as is.
"""

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tideline import brand
from tideline.api import queries
from tideline.brand import human_date
from tideline.config import get_settings
from tideline.db.models import Check, CheckResult, DailyRollup, Site
from tideline.notify.base import Notifier, format_duration
from tideline.observability.logging import log_event
from tideline.reports.rollups import month_rollups

log = logging.getLogger("tideline.reports")

TEMPLATES = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass
class MonthlyReport:
    site_name: str
    domain: str
    site_id: int
    year: int
    month: int
    days_covered: int
    days_in_month: int
    uptime_percent: float | None
    checks_run: int
    p50_ms: int | None
    p95_ms: int | None
    incidents: list[queries.IncidentView] = field(default_factory=list)
    downtime_seconds: int = 0
    daily: list[DailyRollup] = field(default_factory=list)
    watched: list["Watched"] = field(default_factory=list)
    coming_up: list[str] = field(default_factory=list)
    first_day: date | None = None

    @property
    def month_name(self) -> str:
        return date(self.year, self.month, 1).strftime("%B %Y")

    @property
    def downtime_text(self) -> str:
        if not self.downtime_seconds:
            return "none"
        return format_duration(timedelta(seconds=self.downtime_seconds))

    @property
    def uptime_text(self) -> str:
        return brand.percent(self.uptime_percent, empty="no data")

    @property
    def subject(self) -> str:
        return f"{self.month_name} report: {self.site_name}"

    @property
    def uptime_sentence(self) -> str:
        """The line under the big number, in words a client reads."""
        since = (
            f" since Tideline started watching it on {human_date(self.first_day, year=False)}"
            if (
                self.first_day
                and (self.first_day.year, self.first_day.month) == (self.year, self.month)
            )
            else " all month"
        )
        if self.uptime_percent is None:
            return "Tideline has not watched this site yet this month."
        if self.uptime_percent >= 99.999:
            return f"Your website answered every check{since}. It was never down."
        return (
            f"Your website was down for {self.downtime_text} in total{since}. "
            "Every outage is listed below."
        )


@dataclass(frozen=True)
class Watched:
    """One line of "What was watched": plain words, a status and the evidence."""

    tone: brand.Tone
    text: str
    evidence: str


# What each check means to a client, in their words rather than ours: how to
# say it when it is fine, and how to say it when it is not.
CLIENT_WORDS: dict[str, tuple[str, str]] = {
    "uptime": ("The site is up and answers quickly", "The site went down"),
    "content": (
        "The page shows your business, and nothing it shouldn't",
        "The page is not showing what it should",
    ),
    "form": ("The contact form can deliver", "The contact form is not delivering"),
    "links": ("Every link leads somewhere", "A link leads nowhere"),
    "tls": ("The security certificate is valid", "The security certificate needs attention"),
    "domain": ("The domain is registered", "The domain needs renewing"),
    "dns": ("The DNS records have not changed", "The DNS records have changed"),
    "email_auth": (
        "Email from your domain is protected from spoofing",
        "Email from your domain could be spoofed",
    ),
}


def client_words(kind: str, tone: brand.Tone) -> str:
    fine, problem = CLIENT_WORDS.get(kind, (brand.check_name(kind), brand.check_name(kind)))
    return problem if tone in ("warn", "down") else fine


def _evidence(check: queries.CheckStatus) -> str:
    detail = check.detail or {}
    tone = brand.tone(check.status)
    if tone == "none":
        return "not checked yet"
    if check.kind in ("uptime", "content"):
        return "every 5 min" if tone == "up" else "failing"
    if check.kind == "tls" and detail.get("not_after"):
        return "until " + brand.human_date(datetime.fromisoformat(detail["not_after"]), year=False)
    if check.kind == "domain" and detail.get("expiry"):
        return "until " + brand.human_date(date.fromisoformat(detail["expiry"]))
    if check.kind == "links" and detail.get("links_checked") is not None:
        return f"{detail['links_checked']} links"
    if check.kind == "dns":
        return "hourly" if tone == "up" else "changed"
    if check.kind == "email_auth" and tone != "up":
        missing = []
        if not detail.get("spf_records"):
            missing.append("SPF")
        if not detail.get("dmarc_record"):
            missing.append("DMARC")
        return (" and ".join(missing) + " missing") if missing else "needs attention"
    if check.kind == "form":
        return "checked daily" if tone == "up" else "not delivering"
    return "checked daily"


def _coming_up(checks: dict[str, queries.CheckStatus], today: date) -> list[str]:
    """Things a client should know about before they become problems."""
    notes = []
    domain = checks.get("domain")
    if domain and domain.detail and domain.detail.get("expiry"):
        expiry = date.fromisoformat(domain.detail["expiry"])
        days = (expiry - today).days
        if 0 <= days <= 90:
            registrar = (domain.detail.get("registrar") or "").rstrip(".")
            with_whom = f" It is registered with {registrar}." if registrar else ""
            notes.append(
                f"Your domain renews on {brand.human_date(expiry)}.{with_whom} "
                "If it lapses, the website and email stop working."
            )
    tls = checks.get("tls")
    tls_days = tls.detail.get("days_left") if tls and tls.detail else None
    if tls_days is not None and float(tls_days) < 21:
        notes.append(
            "The security certificate expires soon. It normally renews by itself; "
            "this is being watched."
        )
    return notes


def _percentile(values: list[int], fraction: float) -> int | None:
    """Percentile across daily figures. Close enough for a monthly summary."""
    if not values:
        return None
    ordered = sorted(values)
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


async def build_report(session: AsyncSession, site_id: int, year: int, month: int) -> MonthlyReport:
    site = await session.get(Site, site_id)
    if site is None:
        raise LookupError(f"no site with id {site_id}")

    rows = await month_rollups(session, site_id, year, month)
    start = datetime(year, month, 1, tzinfo=UTC)
    end = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    incidents = [
        incident
        for incident in await queries.incidents(session, site_id=site_id, limit=500)
        if incident.opened_at < end and (incident.resolved_at or end) >= start
    ]

    uptime_checks = sum(row.uptime_checks for row in rows)
    uptime_ok = sum(row.uptime_ok for row in rows)

    statuses = await queries.site_statuses(session, site_id)
    checks = {c.kind: c for c in statuses[0].checks if c.enabled} if statuses else {}
    order = {
        k: i
        for i, k in enumerate(
            ["uptime", "content", "form", "links", "tls", "domain", "dns", "email_auth"]
        )
    }
    watched = [
        Watched(brand.tone(c.status), client_words(c.kind, brand.tone(c.status)), _evidence(c))
        for c in sorted(checks.values(), key=lambda c: order.get(c.kind, 99))
    ]
    first_result = await session.scalar(
        select(func.min(CheckResult.started_at))
        .join(Check, Check.id == CheckResult.check_id)
        .where(Check.site_id == site_id)
    )
    first_day = (
        first_result.astimezone(ZoneInfo(get_settings().display_timezone)).date()
        if first_result
        else None
    )
    return MonthlyReport(
        site_name=site.name,
        domain=site.domain,
        site_id=site_id,
        year=year,
        month=month,
        days_covered=len(rows),
        days_in_month=monthrange(year, month)[1],
        uptime_percent=round(100 * uptime_ok / uptime_checks, 3) if uptime_checks else None,
        checks_run=sum(row.results for row in rows),
        p50_ms=_percentile([row.p50_ms for row in rows if row.p50_ms is not None], 0.5),
        p95_ms=_percentile([row.p95_ms for row in rows if row.p95_ms is not None], 0.95),
        incidents=sorted(incidents, key=lambda i: i.opened_at),
        downtime_seconds=sum(row.downtime_seconds for row in rows),
        daily=rows,
        watched=watched,
        coming_up=_coming_up(checks, datetime.now(UTC).date()),
        first_day=first_day,
    )


def _local(when: datetime, fmt: str) -> str:
    return brand.local(when, fmt, get_settings().display_timezone)


TEMPLATES.filters["local"] = _local
TEMPLATES.filters["checkname"] = brand.check_name


def render_html(report: MonthlyReport) -> str:
    return TEMPLATES.get_template("monthly.html").render(
        report=report, format_duration=format_duration
    )


def render_text(report: MonthlyReport) -> str:
    """Plain-text version: the email fallback, and what the CLI prints."""
    marks = {"up": "o", "warn": "!", "down": "x", "none": "-"}
    lines = [
        f"{report.site_name} ({report.domain})",
        f"{report.month_name} website report, from Tideline",
        "",
        f"Uptime: {report.uptime_text}",
        report.uptime_sentence,
        "",
        f"Typical response time: {report.p50_ms or '-'} ms",
        f"Downtime: {report.downtime_text}",
    ]
    if report.coming_up:
        lines += ["", "Coming up:"] + [f"  ! {note}" for note in report.coming_up]
    events = [i for i in report.incidents if i.severity == "critical"]
    if events:
        lines += ["", "What happened:"]
        for incident in events:
            ended = (
                f"fixed after {format_duration(incident.resolved_at - incident.opened_at)}"
                if incident.resolved_at
                else "still open"
            )
            lines.append(
                f"  {_local(incident.opened_at, '%-d %B, %H:%M')} ({ended}): "
                f"{brand.check_name(incident.check_kind)}: {incident.summary}"
            )
    lines += ["", "What was watched:"]
    lines += [f"  {marks[w.tone]} {w.text} ({w.evidence})" for w in report.watched]
    lines += [
        "",
        "Watched every five minutes by Tideline, the monitoring service behind every "
        "OBdesign website.",
    ]
    return "\n".join(lines)


async def sites_for_reports(session: AsyncSession) -> list[int]:
    return list(await session.scalars(select(Site.id).where(Site.active).order_by(Site.name)))


def previous_month(today: date) -> tuple[int, int]:
    """(year, month) of the month before `today`'s."""
    first = today.replace(day=1)
    last_of_previous = first - timedelta(days=1)
    return last_of_previous.year, last_of_previous.month


async def send_month(
    sessionmaker: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    year: int,
    month: int,
) -> int:
    """Build and send every active site's report for one month. Returns how many went.

    One site failing to send does not stop the others: each is logged, and the
    rest carry on.
    """
    sent = 0
    async with sessionmaker() as session:
        for site_id in await sites_for_reports(session):
            report = await build_report(session, site_id, year, month)
            try:
                await notifier.send_report(report.subject, render_text(report), render_html(report))
            except Exception:
                log.exception("report_send_failed", extra={"site": report.domain})
                continue
            sent += 1
    log_event(log, "monthly_reports_sent", month=f"{year}-{month:02d}", sent=sent)
    return sent
