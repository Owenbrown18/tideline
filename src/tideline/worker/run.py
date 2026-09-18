"""One run: check every site once, then tell Owen.

This is the whole of Tideline's scheduled work (docs/decisions/0005). In
production AWS starts it on the 1st and 15th (tideline.aws_lambda.run_handler);
locally `tideline run` does the same against a local database file.

A run has two halves, so that nothing is emailed before it is saved:

    check_all()   1. load the site list, if one is given
                  2. run every enabled check, a few at a time
                  3. summarise today into daily_rollups, purge very old results
    (on Lambda: the database is uploaded to S3 here)
    deliver()     4. email Owen one summary, if anything changed, and only then
                     record those alerts as sent
                  5. on the 1st, email last month's reports

If the upload between them fails, nothing has been emailed, so a rerun cannot
send anything twice. If an email fails, the run reports failure (the Lambda
"run failed" alarm), and an unsent "open" alert is retried by the next run.
"""

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tideline.api.queries import incidents
from tideline.checks import Clients
from tideline.checks.http import PageFetcher, make_client
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
    summary_emailed: bool = False
    reports_sent: int = 0
    email_failed: bool = False
    seconds: float = 0.0


@dataclass
class Outbox:
    """What a run has to tell Owen, held until its results are saved."""

    digest: Digest
    started: datetime
    still_open: list[StillOpen] = field(default_factory=list)
    report_month: tuple[int, int] | None = None


async def check_all(
    settings: Settings,
    notifier: Notifier | None = None,
    *,
    sites_file: Path | None = None,
    kind: str | None = None,
    domain: str | None = None,
    reports: bool | None = None,
    now: datetime | None = None,
    clients: Clients | None = None,
) -> tuple[RunReport, Outbox]:
    """Run every enabled check once and save the results. Sends nothing.

    `kind` and `domain` narrow the run (for re-checking after a fix); a narrowed
    run sends no monthly reports unless `reports=True`. `clients` replaces the
    real network (the showcase data uses a simulated one).
    """
    started = now or datetime.now(UTC)
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    digest = Digest(notifier or build_notifier(settings))
    http = make_client(settings.user_agent, settings.allow_private_addresses)
    clients = clients or Clients(
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
        # Already open before this run, still open, and not news in this run
        # (an escalated incident is listed once, under "New").
        still_open = [
            s for s in await _still_open(sessionmaker, open_before - digest.incident_ids())
        ]
        open_count = await _open_count(sessionmaker)

        narrowed = bool(kind or domain)
        send_reports = (
            reports
            if reports is not None
            else (not narrowed and is_report_day(started, settings.display_timezone))
        )
        report_month = previous_month(started.astimezone(UTC).date()) if send_reports else None

        report = RunReport(
            checks=runner.stats["checks_run"],
            failures=runner.stats["check_failures"],
            errors=runner.stats["check_errors"],
            open_incidents=open_count,
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
        log_event(log, "run_checked", **asdict(report), **maintenance)
        return report, Outbox(digest, started, still_open, report_month)
    finally:
        await http.aclose()
        await engine.dispose()


async def deliver(
    settings: Settings, outbox: Outbox, report: RunReport, sent_at: datetime | None = None
) -> RunReport:
    """Email what the run found, then record what was sent. Updates `report`.

    `sent_at` is when the alerts are recorded as sent: now, unless the run was
    given its own clock (tests and the showcase replay past runs)."""
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        try:
            report.summary_emailed = (
                await outbox.digest.flush(outbox.started, outbox.still_open) is not None
            )
        except Exception:
            report.email_failed = True
            log.exception("run_summary_send_failed")
        else:
            if report.summary_emailed:
                async with sessionmaker() as session, session.begin():
                    await outbox.digest.record(session, sent_at or datetime.now(UTC))

        if outbox.report_month is not None:
            year, month = outbox.report_month
            failed: list[str] = []
            report.reports_sent = await send_month(
                sessionmaker, outbox.digest, year, month, failed=failed
            )
            report.email_failed = report.email_failed or bool(failed)
    finally:
        await engine.dispose()
    log_event(log, "run_delivered", **asdict(report))
    return report


async def run(
    settings: Settings,
    notifier: Notifier | None = None,
    *,
    sites_file: Path | None = None,
    kind: str | None = None,
    domain: str | None = None,
    reports: bool | None = None,
    now: datetime | None = None,
    clients: Clients | None = None,
) -> RunReport:
    """A whole run with nothing to upload in between: `tideline run`, tests, the showcase."""
    report, outbox = await check_all(
        settings,
        notifier,
        sites_file=sites_file,
        kind=kind,
        domain=domain,
        reports=reports,
        now=now,
        clients=clients,
    )
    return await deliver(settings, outbox, report, sent_at=now)


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
