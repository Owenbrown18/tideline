# 0002: APScheduler inside the worker, async throughout

**Status:** accepted, 2026-09-17

**Context.** About 46 scheduled checks, most every 5 minutes, all network-bound.

**Decision.**
- APScheduler 3 (`AsyncIOScheduler`) in the worker process, one interval job
  per check row. Not cron (no per-check intervals or overlap control), not a
  queue like Celery + Redis (a second moving part with nothing to distribute).
- asyncio end to end: `httpx` for HTTP, asyncio streams for TLS, SQLAlchemy's
  async session over psycopg 3. Waiting on the network costs no threads, and
  a semaphore caps concurrent checks (`SITEWATCH_MAX_CONCURRENT_CHECKS`).
- psycopg 3 serves both the async app and Alembic's synchronous migrations
  from one URL.

**Consequences.** Jobs live in memory, not the database: after a restart every
check simply runs again within the first minute, which is what a monitor
should do anyway. Only one worker may run at a time; the partial unique index
on open incidents protects the data if that is ever violated.

**Revisit when** checks need to run from more than one machine.
