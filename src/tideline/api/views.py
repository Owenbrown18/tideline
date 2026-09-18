"""What each screen shows, assembled from the queries.

queries.py is the SQL; this module turns rows into the things the templates
draw: the headline sentence, the 30-day strips, a site's checks in reading
order, an incident's timeline. Keeping the shaping here keeps the templates
free of logic and makes every screen testable without rendering HTML.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tideline import brand
from tideline.api import queries
from tideline.db.models import Alert, DailyRollup

STRIP_DAYS = 30


# --- the 30-day strip -----------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    day: date
    tone: brand.Tone
    uptime: float | None

    @property
    def label(self) -> str:
        when = self.day.strftime("%a %d %b")
        if self.uptime is None:
            return f"{when}: not watched"
        return f"{when}: {self.uptime:.2f}% up"


def cell_tone(uptime: float | None) -> brand.Tone:
    """A day's tone from its uptime. Thresholds match the monthly report."""
    if uptime is None:
        return "none"
    if uptime >= 99.9:
        return "up"
    if uptime >= 99.0:
        return "warn"
    return "down"


async def strips(
    session: AsyncSession, site_ids: list[int], today: date | None = None
) -> dict[int, list[Cell]]:
    """The last 30 days per site, oldest first: rollups for past days, live data for today."""
    today = today or datetime.now(UTC).date()
    first = today - timedelta(days=STRIP_DAYS - 1)
    by_day: dict[tuple[int, date], float | None] = {}

    for rollup in await session.scalars(
        select(DailyRollup).where(
            DailyRollup.site_id.in_(site_ids), DailyRollup.day >= first, DailyRollup.day < today
        )
    ):
        by_day[(rollup.site_id, rollup.day)] = rollup.uptime_percent

    # Today is not rolled up until tomorrow, so it comes straight from results.
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    live = await session.execute(
        text(
            """
            SELECT c.site_id,
                   count(*) AS n,
                   count(*) FILTER (WHERE r.status = 'ok') AS ok
            FROM check_results r
            JOIN checks c ON c.id = r.check_id
            WHERE c.kind = 'uptime' AND r.started_at >= :start
            GROUP BY c.site_id
            """
        ),
        {"start": start},
    )
    for row in live:
        if row.site_id in site_ids and row.n:
            by_day[(row.site_id, today)] = round(100 * row.ok / row.n, 3)

    out: dict[int, list[Cell]] = {}
    for site_id in site_ids:
        cells = []
        for offset in range(STRIP_DAYS):
            day = first + timedelta(days=offset)
            uptime = by_day.get((site_id, day))
            cells.append(Cell(day, cell_tone(uptime), uptime))
        out[site_id] = cells
    return out


def strip_summary(cells: list[Cell]) -> str:
    """Screen-reader text for a strip, since the cells themselves are visual."""
    watched = [c for c in cells if c.uptime is not None]
    if not watched:
        return "No days watched yet."
    bad = [c for c in watched if c.tone != "up"]
    days = "day" if len(watched) == 1 else "days"
    if not bad:
        return f"{len(watched)} {days} watched, all fully up."
    return f"{len(watched)} {days} watched, {len(bad)} with downtime."


# --- the overview -----------------------------------------------------------------


def _days_left(check: queries.CheckStatus | None) -> int | None:
    if check is None or not check.detail:
        return None
    value = check.detail.get("days_left")
    return int(value) if value is not None else None


@dataclass
class SiteRow:
    id: int
    name: str
    domain: str
    tone: brand.Tone
    cells: list[Cell]
    strip_label: str
    uptime: float | None
    p50_ms: int | None
    cert_days: int | None
    domain_days: int | None
    open_incidents: int


@dataclass
class NeedsYou:
    site_id: int
    site_name: str
    domain: str
    check_name: str
    summary: str
    opened_at: datetime


@dataclass
class Overview:
    headline: brand.Headline
    rows: list[SiteRow]
    needs_you: list[NeedsYou]
    warnings: list[NeedsYou]
    total_sites: int
    total_checks: int
    uptime_30d: float | None
    last_checked_at: datetime | None
    state: brand.Tone = "none"
    down_count: int = 0
    warn_count: int = 0
    up_count: int = 0


_TONE_ORDER = {"down": 0, "warn": 1, "none": 2, "up": 3}


async def overview(session: AsyncSession) -> Overview:
    sites = [s for s in await queries.site_statuses(session) if s.active]
    open_incidents = await queries.incidents(session, open_only=True, limit=500)
    site_ids = [s.id for s in sites]
    cells = await strips(session, site_ids) if site_ids else {}

    rows: list[SiteRow] = []
    total_checks = 0
    last_checked: datetime | None = None
    uptime_numerator = uptime_denominator = 0
    for site in sites:
        enabled = [c for c in site.checks if c.enabled]
        total_checks += len(enabled)
        by_kind = {c.kind: c for c in enabled}
        for check in enabled:
            if check.last_checked_at and (not last_checked or check.last_checked_at > last_checked):
                last_checked = check.last_checked_at
        stats = await queries.uptime_stats(session, site.id, STRIP_DAYS)
        uptime_numerator += stats.ok
        uptime_denominator += stats.results
        site_cells = cells.get(site.id, [])
        rows.append(
            SiteRow(
                id=site.id,
                name=site.name,
                domain=site.domain,
                tone=brand.tone(site.status),
                cells=site_cells,
                strip_label=strip_summary(site_cells),
                uptime=stats.uptime_percent,
                p50_ms=stats.p50_ms,
                cert_days=_days_left(by_kind.get("tls")),
                domain_days=_days_left(by_kind.get("domain")),
                open_incidents=site.open_incidents,
            )
        )
    rows.sort(key=lambda r: (_TONE_ORDER[r.tone], r.name.lower()))

    def item(i: queries.IncidentView) -> NeedsYou:
        return NeedsYou(
            site_id=i.site_id,
            site_name=i.site_name,
            domain=i.domain,
            check_name=brand.check_name(i.check_kind),
            summary=i.summary,
            opened_at=i.opened_at,
        )

    needs_you = [item(i) for i in open_incidents if i.severity == "critical"]
    warnings = [item(i) for i in open_incidents if i.severity != "critical"]
    down_sites = [r.name for r in rows if r.tone == "down"]
    warn_sites = [r for r in rows if r.tone == "warn"]
    waiting = bool(rows) and all(r.tone == "none" for r in rows)
    head = brand.headline(len(rows), down_sites, len(warnings), waiting=waiting)
    return Overview(
        headline=head,
        rows=rows,
        needs_you=needs_you,
        warnings=warnings,
        total_sites=len(rows),
        total_checks=total_checks,
        uptime_30d=(100 * uptime_numerator / uptime_denominator if uptime_denominator else None),
        last_checked_at=last_checked,
        state=head.tone,
        down_count=len(down_sites),
        warn_count=len(warn_sites),
        up_count=len([r for r in rows if r.tone == "up"]),
    )


# --- one site -----------------------------------------------------------------------


@dataclass
class CheckCard:
    kind: str
    name: str
    tone: brand.Tone
    summary: str
    last_checked_at: datetime | None
    interval_seconds: int


@dataclass
class TimelineEntry:
    at: datetime
    text: str


@dataclass
class IncidentPanel:
    id: int
    check_kind: str
    check_name: str
    severity: str
    summary: str
    opened_at: datetime
    timeline: list[TimelineEntry]
    guidance: str
    can_accept_dns: bool


@dataclass
class SitePage:
    id: int
    name: str
    domain: str
    tone: brand.Tone
    headline: str
    subline: str
    checks: list[CheckCard]
    cells: list[Cell]
    strip_label: str
    open_incidents: list[IncidentPanel]
    past_incidents: list[queries.IncidentView]
    uptime: queries.UptimeStats
    passing: int
    enabled: int


ALERT_WORDS = {
    "open": "Alert emailed to Owen.",
    "escalated": "Now critical. Alert emailed to Owen.",
    "reminder": "Still open after a day. Reminder emailed.",
    "resolved": "Recovery emailed to Owen.",
}


def alert_words(kind: str, channel: str) -> str:
    """What happened to an alert, truthfully: emailed in production, only logged locally."""
    words = ALERT_WORDS.get(kind, kind)
    if channel != "ses":
        return words.replace("emailed to Owen", "logged (email is off here)").replace(
            "emailed", "logged"
        )
    return words


def _site_headline(tone: brand.Tone, cards: list[CheckCard], name: str) -> str:
    failing = [c for c in cards if c.tone == "down"]
    warning = [c for c in cards if c.tone == "warn"]
    if failing:
        return brand.failing_headline(failing[0].kind, name)
    if warning:
        return (
            "Up, with one thing to look at"
            if len(warning) == 1
            else (f"Up, with {brand.number_word(len(warning)).lower()} things to look at")
        )
    if tone == "none":
        return "Waiting for the first checks"
    return "Everything is fine"


async def site_page(
    session: AsyncSession, site_id: int, zone: str = "America/Vancouver"
) -> SitePage | None:
    statuses = await queries.site_statuses(session, site_id)
    if not statuses:
        return None
    site = statuses[0]
    enabled = [c for c in site.checks if c.enabled]
    order = {kind: i for i, kind in enumerate(brand.CHECK_ORDER)}
    cards = sorted(
        (
            CheckCard(
                kind=c.kind,
                name=brand.check_name(c.kind),
                tone=brand.tone(c.status),
                summary=c.summary or "Waiting for the first result",
                last_checked_at=c.last_checked_at,
                interval_seconds=c.interval_seconds,
            )
            for c in enabled
        ),
        # Problems first, then the usual reading order.
        key=lambda card: (
            _TONE_ORDER[card.tone] if card.tone != "none" else 2,
            order.get(card.kind, 99),
        ),
    )

    open_views = await queries.incidents(session, open_only=True, site_id=site_id)
    panels = []
    for incident in open_views:
        alerts = list(
            await session.scalars(
                select(Alert).where(Alert.incident_id == incident.id).order_by(Alert.sent_at)
            )
        )
        timeline = [TimelineEntry(incident.opened_at, "Problem found. Incident opened.")]
        timeline += [TimelineEntry(a.sent_at, alert_words(a.kind, a.channel)) for a in alerts]
        card = next((c for c in cards if c.kind == incident.check_kind), None)
        if card and card.last_checked_at and card.last_checked_at > incident.opened_at:
            timeline.append(TimelineEntry(card.last_checked_at, "Checked again. Still failing."))
        panels.append(
            IncidentPanel(
                id=incident.id,
                check_kind=incident.check_kind,
                check_name=brand.check_name(incident.check_kind),
                severity=incident.severity,
                summary=incident.summary,
                opened_at=incident.opened_at,
                timeline=sorted(timeline, key=lambda e: e.at),
                guidance=brand.GUIDANCE.get(incident.check_kind, ""),
                can_accept_dns=incident.check_kind == "dns",
            )
        )

    past = [
        i
        for i in await queries.incidents(session, site_id=site_id, limit=20)
        if i.resolved_at is not None
    ]
    site_cells = (await strips(session, [site_id]))[site_id]
    tone = brand.tone(site.status)
    passing = len([c for c in cards if c.tone == "up"])
    watched_since = await session.scalar(
        text(
            "SELECT min(r.started_at) FROM check_results r JOIN checks c ON c.id = r.check_id "
            "WHERE c.site_id = :id"
        ),
        {"id": site_id},
    )
    since = (
        f"watched since {brand.local(watched_since, '%-d %B', zone)}"
        if watched_since
        else "not checked yet"
    )
    return SitePage(
        id=site.id,
        name=site.name,
        domain=site.domain,
        tone=tone,
        headline=_site_headline(tone, cards, site.name),
        subline=f"{passing} of {len(cards)} checks passing  ·  {since}",
        checks=cards,
        cells=site_cells,
        strip_label=strip_summary(site_cells),
        open_incidents=panels,
        past_incidents=past,
        uptime=await queries.uptime_stats(session, site_id, STRIP_DAYS),
        passing=passing,
        enabled=len(cards),
    )


# --- incidents and reports ------------------------------------------------------------


@dataclass
class IncidentsPage:
    open: list[queries.IncidentView]
    resolved: list[queries.IncidentView]


async def incidents_page(session: AsyncSession, days: int = 30) -> IncidentsPage:
    everything = await queries.incidents(session, limit=500)
    since = datetime.now(UTC) - timedelta(days=days)
    open_ = [i for i in everything if i.resolved_at is None]
    open_.sort(key=lambda i: (i.severity != "critical", i.opened_at))
    resolved = [i for i in everything if i.resolved_at and i.resolved_at >= since]
    return IncidentsPage(open=open_, resolved=resolved)


@dataclass
class ReportMonth:
    year: int
    month: int
    label: str
    key: str
    sites: list[dict[str, Any]]


async def reports_page(session: AsyncSession) -> list[ReportMonth]:
    """Every month that has daily rollups, newest first, with the sites in it."""
    rows = (
        await session.execute(
            text(
                """
                SELECT date_trunc('month', r.day)::date AS month, s.id, s.name, s.domain,
                       sum(r.uptime_ok)::float / nullif(sum(r.uptime_checks), 0) * 100 AS uptime
                FROM daily_rollups r
                JOIN sites s ON s.id = r.site_id
                GROUP BY 1, s.id, s.name, s.domain
                ORDER BY 1 DESC, s.name
                """
            )
        )
    ).all()
    months: dict[date, ReportMonth] = {}
    for row in rows:
        month: date = row.month
        if month not in months:
            months[month] = ReportMonth(
                year=month.year,
                month=month.month,
                label=month.strftime("%B %Y"),
                key=f"{month.year}-{month.month:02d}",
                sites=[],
            )
        months[month].sites.append(
            {
                "id": row.id,
                "name": row.name,
                "domain": row.domain,
                "uptime": row.uptime,  # formatted by brand.percent
            }
        )
    return list(months.values())


async def overall_state(session: AsyncSession) -> brand.Tone:
    """The worst tone across every active site, for the favicon."""
    sites = [s for s in await queries.site_statuses(session) if s.active]
    return brand.worst([brand.tone(s.status) for s in sites])
