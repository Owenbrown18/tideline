"""Read queries behind the API and the dashboard.

Kept in one place so the JSON endpoints and the HTML pages always agree, and so
the SQL is easy to read next to the schema.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Row, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sitewatch.db.models import Check, Incident, Site

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


async def _latest_results(session: AsyncSession, site_id: int | None = None) -> dict[int, Row[Any]]:
    """The newest result per check, as {check_id: row}.

    DISTINCT ON is Postgres's direct way to say "one row per check, the newest
    first", and it uses the (check_id, started_at) index.
    """
    where = "WHERE c.site_id = :site_id" if site_id is not None else ""
    sql = text(
        f"""
        SELECT DISTINCT ON (r.check_id)
               r.check_id, r.status, r.started_at, r.duration_ms, r.detail->>'summary' AS summary
        FROM check_results r
        JOIN checks c ON c.id = r.check_id
        {where}
        ORDER BY r.check_id, r.started_at DESC, r.id DESC
        """
    )
    params = {"site_id": site_id} if site_id is not None else {}
    rows = (await session.execute(sql, params)).all()
    return {row.check_id: row for row in rows}


async def site_statuses(session: AsyncSession, site_id: int | None = None) -> list[SiteStatus]:
    sites = list(
        await session.scalars(
            select(Site).where(Site.id == site_id if site_id else text("true")).order_by(Site.name)
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
    since = datetime.now(UTC) - timedelta(days=days)
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS results,
                       count(*) FILTER (WHERE r.status = 'ok') AS ok,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY (r.detail->>'total_ms')::numeric) AS p50,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY (r.detail->>'total_ms')::numeric) AS p95
                FROM check_results r
                JOIN checks c ON c.id = r.check_id
                WHERE c.site_id = :site_id AND c.kind = 'uptime' AND r.started_at >= :since
                """
            ),
            {"site_id": site_id, "since": since},
        )
    ).one()
    incident_row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS incidents,
                       coalesce(sum(extract(epoch FROM
                           coalesce(i.resolved_at, now())
                           - greatest(i.opened_at, :since))), 0) AS seconds
                FROM incidents i
                JOIN checks c ON c.id = i.check_id
                WHERE c.site_id = :site_id AND c.kind = 'uptime'
                  AND coalesce(i.resolved_at, now()) >= :since
                """
            ),
            {"site_id": site_id, "since": since},
        )
    ).one()
    return UptimeStats(
        site_id=site_id,
        days=days,
        since=since,
        results=row.results,
        ok=row.ok,
        uptime_percent=round(100 * row.ok / row.results, 3) if row.results else None,
        p50_ms=round(row.p50) if row.p50 is not None else None,
        p95_ms=round(row.p95) if row.p95 is not None else None,
        incidents=incident_row.incidents,
        downtime_seconds=int(incident_row.seconds),
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


async def recent_results(session: AsyncSession, site_id: int, limit: int = 50) -> list[Row[Any]]:
    return list(
        await session.execute(
            text(
                """
                SELECT c.kind, c.key, r.status, r.started_at, r.duration_ms,
                       r.detail->>'summary' AS summary
                FROM check_results r
                JOIN checks c ON c.id = r.check_id
                WHERE c.site_id = :site_id
                ORDER BY r.started_at DESC, r.id DESC
                LIMIT :limit
                """
            ),
            {"site_id": site_id, "limit": limit},
        )
    )
