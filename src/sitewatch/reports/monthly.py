"""The monthly report for one site.

Built from the daily rollups (uptime, response times) plus the incident records
for the month, so it works even after the raw results have been purged.

It goes to Owen, never to a client: Sitewatch holds no client addresses. Owen
decides what to forward, and the HTML is written so it can be forwarded as is.
"""

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sitewatch.api import queries
from sitewatch.db.models import DailyRollup, Site
from sitewatch.notify.base import format_duration
from sitewatch.reports.rollups import month_rollups

log = logging.getLogger("sitewatch.reports")

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
        return f"{self.uptime_percent:.2f}%" if self.uptime_percent is not None else "no data"

    @property
    def subject(self) -> str:
        return f"[Sitewatch] {self.month_name} report: {self.site_name}"


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
    )


def render_html(report: MonthlyReport) -> str:
    return TEMPLATES.get_template("monthly.html").render(
        report=report, format_duration=format_duration
    )


def render_text(report: MonthlyReport) -> str:
    """Plain-text version, for the email body and for reading in a terminal."""
    lines = [
        f"{report.site_name} ({report.domain})",
        f"{report.month_name} report from Sitewatch",
        "",
        f"Uptime:          {report.uptime_text}",
        f"Checks run:      {report.checks_run:,}",
        f"Response time:   p50 {report.p50_ms or '-'} ms, p95 {report.p95_ms or '-'} ms",
        f"Downtime:        {report.downtime_text}",
        f"Days covered:    {report.days_covered} of {report.days_in_month}",
        "",
    ]
    if report.incidents:
        lines.append(f"Incidents ({len(report.incidents)}):")
        for incident in report.incidents:
            ended = (
                incident.resolved_at.strftime("%d %b %H:%M")
                if incident.resolved_at
                else "still open"
            )
            lines.append(
                f"  {incident.opened_at:%d %b %H:%M} to {ended}"
                f"  [{incident.check_kind}/{incident.severity}] {incident.summary}"
            )
    else:
        lines.append("No incidents this month.")
    lines += ["", f"https://status.obwebdesign.ca/sites/{report.site_id}/view"]
    return "\n".join(lines)


async def sites_for_reports(session: AsyncSession) -> list[int]:
    return list(await session.scalars(select(Site.id).where(Site.active).order_by(Site.name)))
