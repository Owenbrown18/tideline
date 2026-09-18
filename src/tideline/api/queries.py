"""Read queries behind the API and the dashboard.

Kept in one place so the JSON endpoints and the HTML pages always agree, and so
the SQL is easy to read next to the schema.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tideline.db.models import Check, CheckResult, Incident, Site

# The worst status wins when a site has many checks.
STATUS_RANK = {"ok": 0, "warn": 1, "fail": 2}


@dataclass
class CheckStatus:
    check_id: int
    kind: str
    key: str
    enabled: bool
    interval_seconds: int
    status: str | None
    summary: str | None
    last_checked_at: datetime | None
    duration_ms: int | None
    detail: dict[str, Any] | None = None


@dataclass
class SiteStatus:
    id: int
    name: str
    domain: str
    active: bool
    status: str | None  # None until the first result arrives
    open_incidents: int
    checks: list[CheckStatus]

    @property
    def problem_summaries(self) -> list[str]:
        return [c.summary or "" for c in self.checks if c.status and c.status != "ok"]


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile, the same definition as SQL's percentile_cont."""
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def total_ms(detail: dict[str, Any] | None) -> float | None:
    """The page fetch time a result recorded, if it recorded one."""
    value = (detail or {}).get("total_ms")
    return float(value) if isinstance(value, int | float) else None


@dataclass
class LatestResult:
    check_id: int
    status: str
    started_at: datetime
    duration_ms: int
    detail: dict[str, Any]

    @property
    def summary(self) -> str | None:
        summary = self.detail.get("summary")
        return str(summary) if summary is not None else None


async def _latest_results(
    session: AsyncSession, site_id: int | None = None
) -> dict[int, LatestResult]:
    """The newest result per check, as {check_id: result}.

    A window function numbers each check's results newest first, and the
    outer query keeps number 1. It uses the (check_id, started_at) index.
    """
    newest_first = (
        func.row_number()
        .over(
            partition_by=CheckResult.check_id,
            order_by=(CheckResult.started_at.desc(), CheckResult.id.desc()),
        )
        .label("n")
    )
    ranked = select(CheckResult, newest_first).join(Check, Check.id == CheckResult.check_id)
    if site_id is not None:
        ranked = ranked.where(Check.site_id == site_id)
    inner = ranked.subquery()
    newest = aliased(CheckResult, inner)
    rows = (await session.scalars(select(newest).where(inner.c.n == 1))).all()
    return {
        r.check_id: LatestResult(r.check_id, r.status, r.started_at, r.duration_ms, r.detail or {})
        for r in rows
    }


async def site_statuses(session: AsyncSession, site_id: int | None = None) -> list[SiteStatus]:
    sites = list(
        await session.scalars(
            select(Site).where(Site.id == site_id if site_id else true()).order_by(Site.name)
        )
    )
    if not sites:
        return []
    checks = list(
        await session.scalars(
            select(Check)
            .where(Check.site_id.in_([s.id for s in sites]))
            .order_by(Check.kind, Check.key)
        )
    )
    latest = await _latest_results(session, site_id)
    open_counts: dict[int, int] = {
        row.site_id: row.open_count
        for row in (
            await session.execute(
                select(Check.site_id, func.count().label("open_count"))
                .join(Incident, Incident.check_id == Check.id)
                .where(Incident.resolved_at.is_(None))
                .group_by(Check.site_id)
            )
        ).all()
    }

    out = []
    for site in sites:
        site_checks = []
        for check in (c for c in checks if c.site_id == site.id):
            row = latest.get(check.id)
            site_checks.append(
                CheckStatus(
                    check_id=check.id,
                    kind=check.kind,
                    key=check.key,
                    enabled=check.enabled,
                    interval_seconds=check.interval_seconds,
                    status=row.status if row else None,
                    summary=row.summary if row else None,
                    last_checked_at=row.started_at if row else None,
                    duration_ms=row.duration_ms if row else None,
                    detail=row.detail if row else None,
                )
            )
        statuses = [c.status for c in site_checks if c.status and c.enabled]
        worst = max(statuses, key=lambda s: STATUS_RANK[s]) if statuses else None
        out.append(
            SiteStatus(
                id=site.id,
                name=site.name,
                domain=site.domain,
                active=site.active,
                status=worst,
                open_incidents=open_counts.get(site.id, 0),
                checks=site_checks,
            )
        )
    return out


@dataclass
class UptimeStats:
    site_id: int
    days: int
    since: datetime
    results: int
    ok: int
    uptime_percent: float | None
    p50_ms: int | None
    p95_ms: int | None
    incidents: int
    downtime_seconds: int


async def uptime_stats(session: AsyncSession, site_id: int, days: int = 30) -> UptimeStats:
    """Uptime and response times from this site's uptime checks over `days`.

    Response times come from the page fetch recorded in the result detail
    (`total_ms`), not from `duration_ms`: when the uptime and content checks
    share one request, only one of them paid for it.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    results = (
        await session.execute(
            select(CheckResult.status, CheckResult.detail)
            .join(Check, Check.id == CheckResult.check_id)
            .where(
                Check.site_id == site_id,
                Check.kind == "uptime",
                CheckResult.started_at >= since,
            )
        )
    ).all()
    ok = sum(1 for r in results if r.status == "ok")
    times = [t for t in (total_ms(r.detail) for r in results) if t is not None]
    p50, p95 = percentile(times, 0.5), percentile(times, 0.95)

    outages = list(
        await session.scalars(
            select(Incident)
            .join(Check, Check.id == Incident.check_id)
            .where(Check.site_id == site_id, Check.kind == "uptime")
        )
    )
    in_window = [i for i in outages if (i.resolved_at or now) >= since]
    downtime = sum(
        ((i.resolved_at or now) - max(i.opened_at, since)).total_seconds() for i in in_window
    )
    return UptimeStats(
        site_id=site_id,
        days=days,
        since=since,
        results=len(results),
        ok=ok,
        # Rounded down, so 1 failure in 10,000 is 99.99, never a rounded-up 100.0.
        uptime_percent=math.floor(100_000 * ok / len(results)) / 1000 if results else None,
        p50_ms=round(p50) if p50 is not None else None,
        p95_ms=round(p95) if p95 is not None else None,
        incidents=len(in_window),
        downtime_seconds=int(downtime),
    )


@dataclass
class IncidentView:
    id: int
    site_id: int
    site_name: str
    domain: str
    check_kind: str
    check_key: str
    severity: str
    summary: str
    opened_at: datetime
    resolved_at: datetime | None
    last_alerted_at: datetime | None

    @property
    def duration_seconds(self) -> int:
        end = self.resolved_at or datetime.now(UTC)
        return int((end - self.opened_at).total_seconds())


async def incidents(
    session: AsyncSession,
    open_only: bool = False,
    site_id: int | None = None,
    limit: int = 100,
) -> list[IncidentView]:
    query = (
        select(Incident, Check, Site)
        .join(Check, Check.id == Incident.check_id)
        .join(Site, Site.id == Check.site_id)
        .order_by(Incident.opened_at.desc())
        .limit(limit)
    )
    if open_only:
        query = query.where(Incident.resolved_at.is_(None))
    if site_id is not None:
        query = query.where(Site.id == site_id)
    return [
        IncidentView(
            id=incident.id,
            site_id=site.id,
            site_name=site.name,
            domain=site.domain,
            check_kind=check.kind,
            check_key=check.key,
            severity=incident.severity,
            summary=incident.summary,
            opened_at=incident.opened_at,
            resolved_at=incident.resolved_at,
            last_alerted_at=incident.last_alerted_at,
        )
        for incident, check, site in (await session.execute(query)).all()
    ]


async def latest_dns_records(session: AsyncSession, site_id: int) -> dict[str, Any] | None:
    """The records the most recent DNS check saw for this site."""
    details = await session.scalars(
        select(CheckResult.detail)
        .join(Check, Check.id == CheckResult.check_id)
        .where(Check.site_id == site_id, Check.kind == "dns")
        .order_by(CheckResult.started_at.desc(), CheckResult.id.desc())
        .limit(20)
    )
    for detail in details:
        if detail and "records" in detail:
            records: dict[str, Any] = detail["records"]
            return records
    return None


async def resolve_dns_incidents(session: AsyncSession, site_id: int, now: datetime) -> int:
    """Close this site's open DNS incidents. Returns how many were closed."""
    open_dns = list(
        await session.scalars(
            select(Incident)
            .join(Check, Check.id == Incident.check_id)
            .where(Check.site_id == site_id, Check.kind == "dns", Incident.resolved_at.is_(None))
        )
    )
    for incident in open_dns:
        incident.resolved_at = now
        incident.summary = f"{incident.summary} (accepted as the new baseline)"
    return len(open_dns)


async def recent_rollups(session: AsyncSession, site_id: int, days: int = 30) -> list[Any]:
    """The last `days` daily rollups for a site, newest first.

    Read from `daily_rollups`, so the 30-day view still works after raw results
    older than 90 days have been purged.
    """
    from tideline.db.models import DailyRollup

    since = (datetime.now(UTC) - timedelta(days=days)).date()
    return list(
        await session.scalars(
            select(DailyRollup)
            .where(DailyRollup.site_id == site_id, DailyRollup.day >= since)
            .order_by(DailyRollup.day.desc())
        )
    )


async def recent_results(session: AsyncSession, site_id: int, limit: int = 50) -> list[Row[Any]]:
    return list(
        await session.execute(
            select(
                Check.kind,
                Check.key,
                CheckResult.status,
                CheckResult.started_at,
                CheckResult.duration_ms,
                CheckResult.detail,
            )
            .join(Check, Check.id == CheckResult.check_id)
            .where(Check.site_id == site_id)
            .order_by(CheckResult.started_at.desc(), CheckResult.id.desc())
            .limit(limit)
        )
    )
