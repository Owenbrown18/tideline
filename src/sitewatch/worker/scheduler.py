"""The worker process: schedule every enabled check on its interval.

APScheduler keeps one job per check (id "check:<id>"). Every minute the job
list is reconciled with the checks table, so seeding a new site or disabling a
check takes effect without restarting the worker.
"""

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select

from sitewatch.checks import Clients
from sitewatch.checks.http import PageFetcher
from sitewatch.config import Settings
from sitewatch.db.models import Check, Incident, Site
from sitewatch.db.session import make_engine, make_sessionmaker
from sitewatch.notify import Notifier, build_notifier
from sitewatch.observability.logging import log_event
from sitewatch.observability.metrics import emit as emit_metrics
from sitewatch.worker.runner import Runner

log = logging.getLogger("sitewatch.worker")

JOB_PREFIX = "check:"


def first_run_offset(check_id: int, interval_seconds: int) -> int:
    """Spread first runs over the first minute so a restart is not a thundering herd."""
    return (check_id * 7) % max(min(interval_seconds, 60), 1)


class Worker:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.engine = make_engine(settings.database_url)
        self.sessionmaker = make_sessionmaker(self.engine)
        self.http = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=httpx.Timeout(10.0),
        )
        self.clients = Clients(
            http=self.http,
            pages=PageFetcher(self.http),
            uptime_retry_delay_seconds=settings.uptime_retry_delay_seconds,
            rdap_base_url=settings.rdap_base_url,
        )
        self.runner = Runner(
            self.sessionmaker,
            self.clients,
            notifier or build_notifier(settings),
            max_concurrent=settings.max_concurrent_checks,
            reminder_every=timedelta(hours=settings.reminder_hours),
        )
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self._stop = asyncio.Event()

    async def refresh_jobs(self) -> None:
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(Check.id, Check.interval_seconds)
                    .join(Site)
                    .where(Check.enabled, Site.active)
                )
            ).all()
        wanted = {f"{JOB_PREFIX}{row.id}": (row.id, row.interval_seconds) for row in rows}
        now = datetime.now(UTC)
        added = removed = changed = 0

        for job in self.scheduler.get_jobs():
            if not job.id.startswith(JOB_PREFIX):
                continue
            if job.id not in wanted:
                job.remove()
                removed += 1
            elif job.trigger.interval != timedelta(seconds=wanted[job.id][1]):
                job.reschedule(IntervalTrigger(seconds=wanted[job.id][1], timezone=UTC))
                changed += 1

        for job_id, (check_id, interval) in wanted.items():
            if self.scheduler.get_job(job_id) is None:
                self.scheduler.add_job(
                    self.runner.run_check,
                    IntervalTrigger(seconds=interval, timezone=UTC),
                    args=[check_id],
                    id=job_id,
                    next_run_time=now + timedelta(seconds=first_run_offset(check_id, interval)),
                    max_instances=1,  # a slow run is never overlapped by the next one
                    coalesce=True,  # after a stall, run once, not once per missed slot
                    misfire_grace_time=interval,
                )
                added += 1

        if added or removed or changed:
            log_event(
                log,
                "schedule_updated",
                jobs=len(wanted),
                added=added,
                removed=removed,
                changed=changed,
            )

    async def heartbeat(self) -> None:
        async with self.sessionmaker() as session:
            open_incidents = await session.scalar(
                select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None))
            )
        stats = dict(self.runner.stats)
        self.runner.stats.clear()
        # The heartbeat metric is what the "watching the watcher" alarm watches:
        # if this stops arriving for 15 minutes, CloudWatch emails Owen through
        # SNS, which does not depend on this instance or on SES.
        emit_metrics(
            {
                "worker_heartbeat": 1,
                "checks_run": stats.get("checks_run", 0),
                "check_failures": stats.get("check_failures", 0),
                "open_incidents": open_incidents or 0,
            }
        )
        log_event(
            log,
            "worker_heartbeat",
            open_incidents=open_incidents,
            scheduled_checks=sum(
                1 for j in self.scheduler.get_jobs() if j.id.startswith(JOB_PREFIX)
            ),
            checks_run=stats.get("checks_run", 0),
            check_failures=stats.get("check_failures", 0),
            checks_skipped=stats.get("checks_skipped", 0),
            check_errors=stats.get("check_errors", 0),
        )

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        log_event(log, "worker_starting")
        self.scheduler.start()
        await self.refresh_jobs()
        self.scheduler.add_job(
            self.refresh_jobs,
            IntervalTrigger(seconds=self.settings.schedule_refresh_seconds, timezone=UTC),
            id="refresh_jobs",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.heartbeat,
            IntervalTrigger(seconds=self.settings.heartbeat_seconds, timezone=UTC),
            id="heartbeat",
            next_run_time=datetime.now(UTC),
            max_instances=1,
            coalesce=True,
        )
        try:
            await self._stop.wait()
        finally:
            log_event(log, "worker_stopping")
            self.scheduler.shutdown(wait=False)
            await self.http.aclose()
            await self.engine.dispose()
