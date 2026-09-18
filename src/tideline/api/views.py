"""What each screen shows, assembled from the queries.

queries.py is the SQL; this module turns rows into the things the templates
draw: the headline sentence, the strips of recent runs, a site's checks in reading
order, an incident's timeline. Keeping the shaping here keeps the templates
free of logic and makes every screen testable without rendering HTML.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tideline import brand
from tideline.api import queries
from tideline.db.models import Alert, Check, CheckResult, DailyRollup, Site
from tideline.schedule import next_run

# How many runs a strip shows: six months at two runs a month.
STRIP_RUNS = 12


# --- the strip of recent runs -----------------------------------------------------


@dataclass(frozen=True)
class Cell:
    """One run on one site. `day` is None for an empty slot before the first run."""

    day: date | None
    tone: brand.Tone
    uptime_ok: int = 0
    uptime_checks: int = 0

    @property
    def label(self) -> str:
        if self.day is None:
            return "No check yet"
        when = self.day.strftime("%a %-d %b")
        return f"{when}: {_CELL_WORDS[self.tone]}"


_CELL_WORDS: dict[brand.Tone, str] = {
    "up": "up",
    "warn": "down at the first try, up at the retry",
    "down": "down",
    "none": "not checked",
}


def cell_tone(uptime_ok: int, uptime_checks: int) -> brand.Tone:
    """A run's tone: up if every uptime check passed, down if none did."""
    if not uptime_checks:
        return "none"
    if uptime_ok == uptime_checks:
        return "up"
    return "down" if uptime_ok == 0 else "warn"


async def strips(session: AsyncSession, site_ids: list[int]) -> dict[int, list[Cell]]:
    """The last STRIP_RUNS runs per site, oldest first, padded with empty slots.

    Each run's day is summarised into daily_rollups right after the run
    (reports/rollups.after_run), so the rollups are the list of runs.
    """
    by_site: dict[int, list[DailyRollup]] = {site_id: [] for site_id in site_ids}
    for rollup in await session.scalars(
        select(DailyRollup)
        .where(DailyRollup.site_id.in_(site_ids))
        .order_by(DailyRollup.day.desc())
    ):
        runs = by_site[rollup.site_id]
        if len(runs) < STRIP_RUNS:
            runs.append(rollup)

    out: dict[int, list[Cell]] = {}
    for site_id, runs in by_site.items():
        cells = [
            Cell(r.day, cell_tone(r.uptime_ok, r.uptime_checks), r.uptime_ok, r.uptime_checks)
            for r in reversed(runs)
        ]
        out[site_id] = [Cell(None, "none")] * (STRIP_RUNS - len(cells)) + cells
    return out


def strip_summary(cells: list[Cell]) -> str:
    """Screen-reader text for a strip, since the cells themselves are visual."""
    runs = [c for c in cells if c.day is not None]
    if not runs:
        return "Not checked yet."
    up = [c for c in runs if c.tone == "up"]
    checks = "check" if len(runs) == 1 else "checks"
    if len(up) == len(runs):
        return f"Up at all {len(runs)} {checks}." if len(runs) > 1 else "Up at the one check."
    return f"Up at {len(up)} of {len(runs)} {checks}."


def up_at(cells: list[Cell]) -> tuple[int, int]:
    """(runs where the site was up, runs), across a strip."""
    runs = [c for c in cells if c.day is not None]
    return len([c for c in runs if c.tone == "up"]), len(runs)


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
    up_runs: int
    runs: int
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
    next_run: datetime
    last_checked_at: datetime | None
    state: brand.Tone = "none"
    down_count: int = 0
    warn_count: int = 0
    up_count: int = 0


_TONE_ORDER = {"down": 0, "warn": 1, "none": 2, "up": 3}

# Response times are averaged over this window: the last few runs.
RESPONSE_DAYS = 90


async def overview(session: AsyncSession, zone: str = "America/Vancouver") -> Overview:
    sites = [s for s in await queries.site_statuses(session) if s.active]
    open_incidents = await queries.incidents(session, open_only=True, limit=500)
    site_ids = [s.id for s in sites]
    cells = await strips(session, site_ids) if site_ids else {}

    rows: list[SiteRow] = []
    total_checks = 0
    last_checked: datetime | None = None
    for site in sites:
        enabled = [c for c in site.checks if c.enabled]
        total_checks += len(enabled)
        by_kind = {c.kind: c for c in enabled}
        for check in enabled:
            if check.last_checked_at and (not last_checked or check.last_checked_at > last_checked):
                last_checked = check.last_checked_at
        stats = await queries.uptime_stats(session, site.id, RESPONSE_DAYS)
        site_cells = cells.get(site.id, [])
        up_runs, runs = up_at(site_cells)
        rows.append(
            SiteRow(
                id=site.id,
                name=site.name,
                domain=site.domain,
                tone=brand.tone(site.status),
                cells=site_cells,
                strip_label=strip_summary(site_cells),
                up_runs=up_runs,
                runs=runs,
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
        next_run=next_run(datetime.now(UTC), zone),
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
    up_runs: int
    runs: int
    passing: int
    enabled: int


ALERT_WORDS = {
    "open": "Alert emailed to Owen.",
    "escalated": "Now critical. Alert emailed to Owen.",
    "reminder": "Still open. Listed in the run summary.",
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
        select(func.min(CheckResult.started_at))
        .join(Check, Check.id == CheckResult.check_id)
        .where(Check.site_id == site_id)
    )
    first_rollup = await session.scalar(
        select(func.min(DailyRollup.day)).where(DailyRollup.site_id == site_id)
    )
    if first_rollup is not None:
        since = f"watched since {brand.human_date(first_rollup, year=False)}"
    elif watched_since is not None:
        since = f"watched since {brand.local(watched_since, '%-d %B', zone)}"
    else:
        since = "not checked yet"
    up_runs, runs = up_at(site_cells)
    return SitePage(
        id=site.id,
        name=site.name,
        domain=site.domain,
        tone=tone,
        headline=_site_headline(tone, cards, site.name),
        subline=since,
        checks=cards,
        cells=site_cells,
        strip_label=strip_summary(site_cells),
        open_incidents=panels,
        past_incidents=past,
        uptime=await queries.uptime_stats(session, site_id, RESPONSE_DAYS),
        up_runs=up_runs,
        runs=runs,
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
            select(DailyRollup, Site)
            .join(Site, Site.id == DailyRollup.site_id)
            .order_by(DailyRollup.day.desc(), Site.name)
        )
    ).all()
    months: dict[date, ReportMonth] = {}
    totals: dict[tuple[date, int], list[int]] = {}
    for rollup, site in rows:
        month = rollup.day.replace(day=1)
        if month not in months:
            months[month] = ReportMonth(
                year=month.year,
                month=month.month,
                label=month.strftime("%B %Y"),
                key=f"{month.year}-{month.month:02d}",
                sites=[],
            )
        key = (month, site.id)
        if key not in totals:
            totals[key] = [0, 0]
            months[month].sites.append({"id": site.id, "name": site.name, "domain": site.domain})
        # Runs, as on the strip: up at a run when every uptime check passed.
        if rollup.uptime_checks:
            totals[key][0] += int(rollup.uptime_ok == rollup.uptime_checks)
            totals[key][1] += 1
    for month, report in months.items():
        report.sites.sort(key=lambda entry: str(entry["name"]).lower())
        for entry in report.sites:
            ok, checks = totals[(month, int(entry["id"]))]
            entry["up_runs"], entry["runs"] = ok, checks
    return list(months.values())


async def overall_state(session: AsyncSession) -> brand.Tone:
    """The worst tone across every active site, for the favicon."""
    sites = [s for s in await queries.site_statuses(session) if s.active]
    return brand.worst([brand.tone(s.status) for s in sites])
