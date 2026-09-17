"""Run one check: execute it, store the result, update incidents.

The database session is not held open while the check runs (an uptime check
can take 40 s with its retry), only for the short write afterwards.
"""

import asyncio
import logging
import time
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from sitewatch.checks import REGISTRY, Clients, Result
from sitewatch.db.models import Check, CheckResult
from sitewatch.incidents.service import process_result
from sitewatch.notify.base import Notifier
from sitewatch.observability.logging import log_event

log = logging.getLogger("sitewatch.runner")


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
        reminder_every: timedelta = timedelta(hours=24),
    ) -> None:
        self.sessionmaker = sessionmaker
        self.clients = clients
        self.notifier = notifier
        self.reminder_every = reminder_every
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Counts since the last heartbeat; the worker reads and resets them.
        self.stats: Counter[str] = Counter()

    async def _load(self, session: AsyncSession, check_id: int) -> Check | None:
        return await session.get(Check, check_id, options=[selectinload(Check.site)])

    async def run_check(self, check_id: int) -> Result | None:
        async with self.sessionmaker() as session:
            check = await self._load(session, check_id)
        if check is None or not check.enabled or not check.site.active:
            return None
        check_fn = REGISTRY.get(check.kind)
        if check_fn is None:
            log_event(log, "check_kind_unknown", logging.ERROR, check_id=check_id, kind=check.kind)
            return None

        async with self._semaphore:
            started_at = self.clients.now()
            t0 = time.perf_counter()
            try:
                result = await check_fn(build_config(check), self.clients)
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

        async with self.sessionmaker() as session, session.begin():
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
