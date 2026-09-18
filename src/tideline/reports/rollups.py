"""Daily rollups and retention.

After each run, every site's day is summarised into one `daily_rollups` row
(checks passed, response-time percentiles, incidents), kept forever: it is what
the dashboard's strip and the monthly report read. Raw results older than
RAW_RESULT_RETENTION_DAYS are deleted, which keeps the database file small.

The rollup is idempotent: running it again for the same day recomputes and
replaces that day's row, so a second run on one day or a backfill is safe.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tideline.api.queries import percentile, total_ms
from tideline.db.models import Check, CheckResult, DailyRollup, Incident, Site
from tideline.observability.logging import log_event

log = logging.getLogger("tideline.rollups")

# About a year of runs. At two runs a month that is roughly 2,000 rows, so this
# only stops the file growing forever; the rollups keep the history.
RAW_RESULT_RETENTION_DAYS = 400


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC start and end of a day. Everything in Tideline is UTC."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def rollup_site_day(session: AsyncSession, site_id: int, day: date) -> DailyRollup | None:
    """Summarise one site's day. Returns None when it has no results that day."""
    start, end = day_bounds(day)
    results = (
        await session.execute(
            select(Check.kind, CheckResult.status, CheckResult.detail)
            .join(Check, Check.id == CheckResult.check_id)
            .where(
                Check.site_id == site_id,
                CheckResult.started_at >= start,
                CheckResult.started_at < end,
            )
        )
    ).all()
    if not results:
        return None

    uptime = [r for r in results if r.kind == "uptime"]
    uptime_ok = sum(1 for r in uptime if r.status == "ok")
    times = [t for t in (total_ms(r.detail) for r in uptime) if t is not None]
    p50, p95 = percentile(times, 0.5), percentile(times, 0.95)

    incidents = list(
        await session.execute(
            select(Incident.opened_at, Incident.resolved_at, Check.kind)
            .join(Check, Check.id == Incident.check_id)
            .where(Check.site_id == site_id, Incident.opened_at < end)
        )
    )
    overlapping = [i for i in incidents if (i.resolved_at or end) >= start]
    downtime = sum(
        (min(i.resolved_at or end, end) - max(i.opened_at, start)).total_seconds()
        for i in overlapping
        if i.kind == "uptime"
    )

    values = {
        "results": len(results),
        "ok_results": sum(1 for r in results if r.status == "ok"),
        "uptime_checks": len(uptime),
        "uptime_ok": uptime_ok,
        "uptime_percent": round(100 * uptime_ok / len(uptime), 3) if uptime else None,
        "p50_ms": round(p50) if p50 is not None else None,
        "p95_ms": round(p95) if p95 is not None else None,
        "incidents_opened": sum(1 for i in overlapping if i.opened_at >= start),
        "downtime_seconds": round(downtime),
        "computed_at": datetime.now(UTC),
    }
    # Update the day's row if it exists, otherwise add it (the unique
    # constraint on site_id and day guarantees there is at most one).
    rollup = await session.scalar(
        select(DailyRollup).where(DailyRollup.site_id == site_id, DailyRollup.day == day)
    )
    if rollup is None:
        rollup = DailyRollup(site_id=site_id, day=day, **values)
        session.add(rollup)
    else:
        for name, value in values.items():
            setattr(rollup, name, value)
    await session.flush()
    return rollup


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


async def after_run(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """What a run does once its checks are done: summarise today, purge old raw results."""
    now = now or datetime.now(UTC)
    sites = await rollup_day(session, now.date())
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
