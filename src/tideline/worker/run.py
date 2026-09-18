"""One run: check every site once, then tidy up and report.

This is the whole of Tideline's scheduled work (docs/decisions/0005). In
production AWS starts it on the 1st and 15th (tideline.aws_lambda.run_handler);
locally `tideline run` does the same against a local database file.

    1. load the site list, if one is given (adds, updates and retires checks)
    2. run every enabled check, a few at a time
    3. summarise today into daily_rollups, purge very old raw results
    4. email Owen one summary, if anything changed
    5. on the 1st, email last month's reports
"""

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tideline.api.queries import incidents
from tideline.checks import Clients
from tideline.checks.http import PageFetcher
from tideline.config import Settings
from tideline.db.models import Check, Incident, Site
from tideline.db.session import make_engine, make_sessionmaker
from tideline.notify import Notifier, build_notifier
from tideline.notify.digest import Digest, StillOpen
from tideline.observability.logging import log_event
from tideline.observability.metrics import emit as emit_metrics
from tideline.reports.monthly import previous_month, send_month
from tideline.reports.rollups import after_run
from tideline.schedule import is_report_day
from tideline.sites import load_sites_file, seed
from tideline.worker.runner import Runner

log = logging.getLogger("tideline.run")


@dataclass
class RunReport:
    checks: int
    failures: int
    errors: int
    open_incidents: int
    summary_emailed: bool
    reports_sent: int
    seconds: float


async def run(
    settings: Settings,
    notifier: Notifier | None = None,
    *,
    sites_file: Path | None = None,
    kind: str | None = None,
    domain: str | None = None,
    reports: bool | None = None,
    now: datetime | None = None,
) -> RunReport:
    """Run every enabled check once. `kind` and `domain` narrow it (for testing a
    fix); `reports` forces the monthly reports on or off (default: on the 1st)."""
    started = now or datetime.now(UTC)
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    digest = Digest(notifier or build_notifier(settings))
    http = httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, timeout=10.0)
    clients = Clients(
        http=http,
        pages=PageFetcher(http),
        uptime_retry_delay_seconds=settings.uptime_retry_delay_seconds,
        rdap_base_url=settings.rdap_base_url,
    )
    runner = Runner(sessionmaker, clients, digest, max_concurrent=settings.max_concurrent_checks)
    try:
        if sites_file is not None:
            async with sessionmaker() as session, session.begin():
                seeded = await seed(session, load_sites_file(sites_file))
            log_event(log, "seeded", **asdict(seeded))

        async with sessionmaker() as session:
            query = select(Check.id).join(Site).where(Check.enabled, Site.active)
            if kind:
                query = query.where(Check.kind == kind)
            if domain:
                query = query.where(Site.domain == domain)
            check_ids = list(await session.scalars(query.order_by(Check.id)))
            open_before = set(
                await session.scalars(select(Incident.id).where(Incident.resolved_at.is_(None)))
            )

        log_event(log, "run_starting", checks=len(check_ids), kind=kind, site=domain)
        await asyncio.gather(*(runner.run_check(check_id) for check_id in check_ids))

        async with sessionmaker() as session, session.begin():
            maintenance = await after_run(session, started)
        still_open = await _still_open(sessionmaker, open_before)
        open_count = await _open_count(sessionmaker)

        emailed = False
        try:
            emailed = await digest.flush(started, still_open) is not None
        except Exception:
            # The results are saved either way; the dashboard shows them.
            log.exception("run_summary_send_failed")

        sent = 0
        if reports if reports is not None else is_report_day(started, settings.display_timezone):
            year, month = previous_month(started.astimezone(UTC).date())
            sent = await send_month(sessionmaker, digest, year, month)

        report = RunReport(
            checks=runner.stats["checks_run"],
            failures=runner.stats["check_failures"],
            errors=runner.stats["check_errors"],
            open_incidents=open_count,
            summary_emailed=emailed,
            reports_sent=sent,
            seconds=round((datetime.now(UTC) - started).total_seconds(), 1),
        )
        emit_metrics(
            {
                "checks_run": report.checks,
                "check_failures": report.failures,
                "check_errors": report.errors,
                "open_incidents": report.open_incidents,
            }
        )
        log_event(log, "run_finished", **asdict(report), **maintenance)
        return report
    finally:
        await http.aclose()
        await engine.dispose()


Sessions = async_sessionmaker[AsyncSession]


async def _still_open(sessionmaker: Sessions, open_before: set[int]) -> list[StillOpen]:
    """Incidents that were open before this run and still are, for the summary."""
    async with sessionmaker() as session:
        views = await incidents(session, open_only=True, limit=500)
    return [
        StillOpen(v.site_name, v.check_kind, v.severity, v.summary, v.opened_at)
        for v in sorted(views, key=lambda v: (v.severity != "critical", v.site_name))
        if v.id in open_before
    ]


async def _open_count(sessionmaker: Sessions) -> int:
    async with sessionmaker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None))
        )
    return int(count or 0)
