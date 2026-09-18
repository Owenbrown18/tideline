"""Daily rollups and retention.

`check_results` grows by roughly 3,500 rows a day at 11 sites. Keeping every
row forever would make the table the biggest thing on the disk and the monthly
report the slowest query in the system, so:

- once a day, each site's previous day is summarised into one `daily_rollups`
  row (uptime, response-time percentiles, incidents, downtime), kept forever;
- raw results older than 90 days are deleted.

The rollup is idempotent: running it again for the same day recomputes and
replaces that day's row, so a backfill or a rerun after a fix is safe.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tideline.db.models import CheckResult, DailyRollup, Site
from tideline.observability.logging import log_event

log = logging.getLogger("tideline.rollups")

RAW_RESULT_RETENTION_DAYS = 90


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start and end of a day. Everything in Tideline is UTC."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def rollup_site_day(session: AsyncSession, site_id: int, day: date) -> DailyRollup | None:
    """Summarise one site's day. Returns None when it has no results that day."""
    start, end = day_bounds(day)
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS results,
                       count(*) FILTER (WHERE r.status = 'ok') AS ok_results,
                       count(*) FILTER (WHERE c.kind = 'uptime') AS uptime_checks,
                       count(*) FILTER (WHERE c.kind = 'uptime' AND r.status = 'ok') AS uptime_ok,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY (r.detail->>'total_ms')::numeric)
                           FILTER (WHERE c.kind = 'uptime') AS p50,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY (r.detail->>'total_ms')::numeric)
                           FILTER (WHERE c.kind = 'uptime') AS p95
                FROM check_results r
                JOIN checks c ON c.id = r.check_id
                WHERE c.site_id = :site_id
                  AND r.started_at >= :start AND r.started_at < :end
                """
            ),
            {"site_id": site_id, "start": start, "end": end},
        )
    ).one()
    if not row.results:
        return None

    incidents = (
        await session.execute(
            text(
                """
                SELECT count(*) FILTER (WHERE i.opened_at >= :start AND i.opened_at < :end)
                           AS opened,
                       coalesce(sum(
                           extract(epoch FROM
                               least(coalesce(i.resolved_at, :end), :end)
                               - greatest(i.opened_at, :start))
                       ) FILTER (WHERE c.kind = 'uptime'), 0) AS downtime
                FROM incidents i
                JOIN checks c ON c.id = i.check_id
                WHERE c.site_id = :site_id
                  AND i.opened_at < :end
                  AND coalesce(i.resolved_at, :end) >= :start
                """
            ),
            {"site_id": site_id, "start": start, "end": end},
        )
    ).one()

    values = {
        "site_id": site_id,
        "day": day,
        "results": row.results,
        "ok_results": row.ok_results,
        "uptime_checks": row.uptime_checks,
        "uptime_ok": row.uptime_ok,
        "uptime_percent": (
            round(100 * row.uptime_ok / row.uptime_checks, 3) if row.uptime_checks else None
        ),
        "p50_ms": round(row.p50) if row.p50 is not None else None,
        "p95_ms": round(row.p95) if row.p95 is not None else None,
        "incidents_opened": incidents.opened,
        "downtime_seconds": round(incidents.downtime),
        "computed_at": datetime.now(UTC),
    }
    statement = (
        insert(DailyRollup)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["site_id", "day"],
            set_={k: v for k, v in values.items() if k not in ("site_id", "day")},
        )
        .returning(DailyRollup)
    )
    return (await session.scalars(statement)).one()


async def rollup_day(session: AsyncSession, day: date) -> int:
    """Summarise every site for one day. Returns how many rows were written."""
    site_ids = list(await session.scalars(select(Site.id).order_by(Site.id)))
    written = 0
    for site_id in site_ids:
        if await rollup_site_day(session, site_id, day) is not None:
            written += 1
    log_event(log, "rollup_day", day=day.isoformat(), sites=written)
    return written


async def purge_old_results(
    session: AsyncSession, now: datetime, retention_days: int = RAW_RESULT_RETENTION_DAYS
) -> int:
    """Delete raw results older than the retention window. Rollups keep the history."""
    cutoff = now - timedelta(days=retention_days)
    deleted = await session.execute(delete(CheckResult).where(CheckResult.started_at < cutoff))
    count: int = getattr(deleted, "rowcount", 0) or 0
    if count:
        log_event(log, "purged_old_results", rows=count, cutoff=cutoff.isoformat())
    return count


async def daily_maintenance(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """What the worker runs once a day: roll up yesterday, purge old raw results."""
    now = now or datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).date()
    sites = await rollup_day(session, yesterday)
    purged = await purge_old_results(session, now)
    return {"sites_rolled_up": sites, "results_purged": purged}


async def month_rollups(
    session: AsyncSession, site_id: int, year: int, month: int
) -> list[DailyRollup]:
    """Every rollup row for one site in one calendar month, oldest first."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return list(
        await session.scalars(
            select(DailyRollup)
            .where(DailyRollup.site_id == site_id, DailyRollup.day >= start, DailyRollup.day < end)
            .order_by(DailyRollup.day)
        )
    )


async def rollup_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(DailyRollup)) or 0
