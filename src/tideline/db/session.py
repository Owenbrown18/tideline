"""Async database engine and session factory.

The database is one SQLite file (docs/decisions/0005). Two settings matter:

- foreign keys are off by default in SQLite, so every connection turns them on,
  or deleting a site would leave its checks and results behind;
- a busy timeout, so a connection waits briefly for another's write to finish
  instead of failing with "database is locked".
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def sync_url(database_url: str) -> str:
    """The same database for synchronous tools (Alembic): drop the async driver."""
    return database_url.replace("+aiosqlite", "")
