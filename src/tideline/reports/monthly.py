"""The monthly report for one site.

Built from the daily rollups (one per site per run: checks passed, response
times) plus the incident records for the month, so it works even after the raw
results have been purged.

Tideline checks twice a month (docs/decisions/0005), so the report says what
was measured, "up at 2 of 2 checks, on 1 and 15 September", rather than an
uptime percentage or a downtime total that two checks cannot know.

It goes to Owen, never to a client: Tideline holds no client addresses. Owen
decides what to forward, and the HTML is written so it can be forwarded as is.
"""

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tideline import brand
from tideline.api import queries
from tideline.brand import human_date
from tideline.config import get_settings
from tideline.db.models import DailyRollup, Site
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
    up_runs: int
    check_days: list[date]
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
    def runs(self) -> int:
        return len(self.check_days)

    @property
    def all_up(self) -> bool:
        return bool(self.runs) and self.up_runs == self.runs

    @property
    def uptime_text(self) -> str:
        """The big figure: up at "2 of 2" checks this month.

        A check is one run: the site counts as up at it when every uptime
        check that run passed (the same rule as the dashboard's strip).
        """
        if not self.runs:
            return "no checks"
        return f"{self.up_runs} of {self.runs}"

    @property
    def subject(self) -> str:
        return f"{self.month_name} report: {self.site_name}"

    @property
    def uptime_sentence(self) -> str:
        """The line under the big figure, in words a client reads."""
        if not self.check_days:
            return "Tideline has not checked this site yet this month."
        runs = len(self.check_days)
        times = {1: "once", 2: "twice"}.get(runs, f"{runs} times")
        dates = _join_dates(self.check_days)
        checked = f"Tideline checked your website {times} this month, on {dates}."
        if self.all_up:
            every = {1: "It was up.", 2: "It was up both times."}.get(runs, "It was up every time.")
            return f"{checked} {every}"
        if runs == 1:
            return f"{checked} It was down at that check; what happened is below."
        return f"{checked} It was up at {self.up_runs} of them; what happened is below."


def _join_dates(days: list[date]) -> str:
    """[1 Sep, 15 Sep] -> "1 and 15 September"; across months, each gets its month."""
    if len({d.month for d in days}) == 1:
        numbers = [str(d.day) for d in days]
        joined = (
            numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " and " + numbers[-1]
        )
        return f"{joined} {days[0].strftime('%B')}"
    names = [human_date(d, year=False) for d in days]
    return ", ".join(names[:-1]) + " and " + names[-1]


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
        return "at every check" if tone == "up" else "failing"
    if check.kind == "tls" and detail.get("not_after"):
        return "until " + brand.human_date(datetime.fromisoformat(detail["not_after"]), year=False)
    if check.kind == "domain" and detail.get("expiry"):
        return "until " + brand.human_date(date.fromisoformat(detail["expiry"]))
    if check.kind == "links" and detail.get("links_checked") is not None:
        return f"{detail['links_checked']} links"
    if check.kind == "dns":
        return "unchanged" if tone == "up" else "changed"
    if check.kind == "email_auth" and tone != "up":
        missing = []
        if not detail.get("spf_records"):
            missing.append("SPF")
        if not detail.get("dmarc_record"):
            missing.append("DMARC")
        return (" and ".join(missing) + " missing") if missing else "needs attention"
    if check.kind == "form":
        return "checked" if tone == "up" else "not delivering"
    return "checked"


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
    runs = [row for row in rows if row.uptime_checks]  # days a run checked the site

    # What was true at the end of the report's month, not today: an August
    # report read in October still describes August (found by review).
    statuses = await queries.site_statuses(session, site_id, before=end)
    checks = {c.kind: c for c in statuses[0].checks if c.enabled} if statuses else {}
    order = {
        k: i
        for i, k in enumerate(
            ["uptime", "content", "form", "links", "tls", "domain", "dns", "email_auth"]
        )
    }
    # Uptime is a statement about the whole month, so it comes from every run;
    # the other checks describe how things stood at the month's end.
    up_runs = sum(1 for r in runs if r.uptime_ok == r.uptime_checks)
    watched = []
    for c in sorted(checks.values(), key=lambda c: order.get(c.kind, 99)):
        tone = brand.tone(c.status)
        evidence = _evidence(c)
        if c.kind == "uptime" and runs:
            tone = "up" if up_runs == len(runs) else "down"
            evidence = (
                "at every check"
                if tone == "up"
                else f"down at {len(runs) - up_runs} of {len(runs)}"
            )
        watched.append(Watched(tone, client_words(c.kind, tone), evidence))
    first_day = await session.scalar(
        select(func.min(DailyRollup.day)).where(DailyRollup.site_id == site_id)
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
        up_runs=up_runs,
        check_days=[r.day for r in runs],
        checks_run=sum(row.results for row in rows),
        p50_ms=_percentile([row.p50_ms for row in rows if row.p50_ms is not None], 0.5),
        p95_ms=_percentile([row.p95_ms for row in rows if row.p95_ms is not None], 0.95),
        incidents=sorted(incidents, key=lambda i: i.opened_at),
        downtime_seconds=sum(row.downtime_seconds for row in rows),
        daily=rows,
        watched=watched,
        coming_up=_coming_up(checks, min(datetime.now(UTC), end - timedelta(days=1)).date()),
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
        f"Up at: {report.uptime_text} {'check' if report.runs == 1 else 'checks'}",
        report.uptime_sentence,
        "",
        f"Typical response time: {report.p50_ms or '-'} ms",
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
        "Checked on the 1st and 15th of every month by Tideline, the monitoring service "
        "behind every OBdesign website.",
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
    failed: list[str] | None = None,
) -> int:
    """Build and send every active site's report for one month. Returns how many went.

    Domains whose report could not be sent are added to `failed`, if given.

    One site failing to send does not stop the others: each is logged, and the
    rest carry on. A site Tideline did not check that month (added since) gets
    no report: an empty one would only confuse a client.
    """
    sent = 0
    async with sessionmaker() as session:
        for site_id in await sites_for_reports(session):
            report = await build_report(session, site_id, year, month)
            if not report.daily:
                log_event(log, "report_skipped_no_checks", site=report.domain)
                continue
            try:
                await notifier.send_report(report.subject, render_text(report), render_html(report))
            except Exception:
                log.exception("report_send_failed", extra={"site": report.domain})
                if failed is not None:
                    failed.append(report.domain)
                continue
            sent += 1
    log_event(log, "monthly_reports_sent", month=f"{year}-{month:02d}", sent=sent)
    return sent
