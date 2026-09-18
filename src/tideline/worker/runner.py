"""Run one check: execute it, store the result, update incidents.

The database session is not held open while the check runs (an uptime check
can take 40 s with its retry), only for the short write afterwards. Checks run
concurrently, but their writes take turns: SQLite allows one writer at a time,
and queueing here is cheaper than retrying "database is locked".
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from tideline.checks import REGISTRY, Clients, Result
from tideline.db.baselines import get_baseline, set_baseline
from tideline.db.models import Check, CheckResult
from tideline.incidents.service import process_result
from tideline.notify.base import Notifier
from tideline.observability.logging import log_event
from tideline.observability.metrics import emit as emit_metrics

log = logging.getLogger("tideline.runner")

# The most one check may take, start to finish. httpx's timeouts apply to each
# step of a request, so without this a server dripping one byte every few
# seconds could hold a check (and the run) until Lambda gives up. The uptime
# check needs room for its 30-second retry; the link crawl for its pages.
DEADLINE_SECONDS = {"links": 240.0, "uptime": 90.0}
DEFAULT_DEADLINE_SECONDS = 60.0


def build_config(check: Check) -> dict[str, Any]:
    """Site-level facts first, then the check's own config on top."""
    return {
        "domain": check.site.domain,
        "name": check.site.name,
        "expected_text": check.site.expected_text,
        **check.config,
    }


class Runner:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        clients: Clients,
        notifier: Notifier,
        max_concurrent: int = 5,
        reminder_every: timedelta | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.clients = clients
        self.notifier = notifier
        self.reminder_every = reminder_every
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._write_lock = asyncio.Lock()
        # Counts for this run, reported at the end of it.
        self.stats: Counter[str] = Counter()

    async def _load(self, session: AsyncSession, check_id: int) -> Check | None:
        return await session.get(Check, check_id, options=[selectinload(Check.site)])

    async def run_check(self, check_id: int) -> Result | None:
        async with self.sessionmaker() as session:
            check = await self._load(session, check_id)
            # The DNS check compares against the accepted baseline, which only
            # the database knows. Load it here and pass it in as config, so the
            # check itself stays a pure function.
            baseline = (
                await get_baseline(session, check.site_id)
                if check and check.kind == "dns"
                else None
            )
        if check is None or not check.enabled or not check.site.active:
            return None
        check_fn = REGISTRY.get(check.kind)
        if check_fn is None:
            log_event(log, "check_kind_unknown", logging.ERROR, check_id=check_id, kind=check.kind)
            return None

        async with self._semaphore:
            started_at = self.clients.now()
            t0 = time.perf_counter()
            config = build_config(check)
            if check.kind == "dns" and baseline is not None:
                config["baseline"] = baseline
            deadline = DEADLINE_SECONDS.get(check.kind, DEFAULT_DEADLINE_SECONDS)
            try:
                async with asyncio.timeout(deadline):
                    result = await check_fn(config, self.clients)
            except TimeoutError:
                self.stats["check_errors"] += 1
                log_event(
                    log,
                    "check_timeout",
                    logging.ERROR,
                    site=check.site.domain,
                    check_kind=check.kind,
                    check_key=check.key,
                    seconds=deadline,
                )
                return None
            except Exception:
                # A bug in a check must not look like an outage, and must not be silent.
                self.stats["check_errors"] += 1
                log.exception(
                    "check_error",
                    extra={
                        "site": check.site.domain,
                        "check_kind": check.kind,
                        "check_key": check.key,
                    },
                )
                return None
            duration_ms = round((time.perf_counter() - t0) * 1000)

        self.stats["checks_run"] += 1
        if result is None:
            self.stats["checks_skipped"] += 1
            log_event(
                log,
                "check_skipped",
                site=check.site.domain,
                check_kind=check.kind,
                check_key=check.key,
                duration_ms=duration_ms,
            )
            return None

        async with self._write_lock, self.sessionmaker() as session, session.begin():
            check = await self._load(session, check_id)
            if check is None:
                return None  # deleted while it ran
            row = CheckResult(
                check_id=check.id,
                started_at=started_at,
                duration_ms=duration_ms,
                status=result.status,
                detail={"summary": result.summary, **result.detail},
            )
            session.add(row)
            await session.flush()
            if result.detail.get("baseline_captured"):
                # First DNS run for this site: what it found becomes the baseline.
                await set_baseline(session, check.site_id, result.detail["records"], started_at)
            decision = await process_result(
                session,
                check,
                row,
                result.summary,
                self.notifier,
                self.clients.now(),
                self.reminder_every,
            )

        if result.status != "ok":
            self.stats["check_failures"] += 1
        emit_metrics({"check_duration_ms": duration_ms})
        log_event(
            log,
            "check_result",
            site=check.site.domain,
            check_kind=check.kind,
            check_key=check.key,
            status=result.status,
            duration_ms=duration_ms,
            summary=result.summary,
            incident_action=decision.action.value,
        )
        return result
