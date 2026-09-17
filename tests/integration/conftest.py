"""Integration tests run against a real Postgres.

Point SITEWATCH_TEST_DATABASE_URL at an empty, throwaway database, e.g. with
`docker compose up -d postgres`:

    SITEWATCH_TEST_DATABASE_URL=postgresql+psycopg://sitewatch:sitewatch@localhost:5432/sitewatch_test

Every table in it is emptied between tests. Without the variable these tests
are skipped locally; CI sets SITEWATCH_REQUIRE_DB=1 so a missing database fails
the build instead of silently skipping.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sitewatch.db.session import make_engine, make_sessionmaker

REPO = Path(__file__).resolve().parents[2]
DATABASE_URL = os.environ.get("SITEWATCH_TEST_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)
            if not DATABASE_URL:
                if os.environ.get("SITEWATCH_REQUIRE_DB"):
                    raise pytest.UsageError(
                        "SITEWATCH_REQUIRE_DB is set but SITEWATCH_TEST_DATABASE_URL is not"
                    )
                item.add_marker(pytest.mark.skip(reason="SITEWATCH_TEST_DATABASE_URL not set"))


def alembic_config() -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL or "")
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    assert DATABASE_URL
    config = alembic_config()
    # Start from nothing so the migrations themselves are under test.
    await asyncio.to_thread(command.downgrade, config, "base")
    await asyncio.to_thread(command.upgrade, config, "head")
    eng = make_engine(DATABASE_URL)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE sites, checks, check_results, incidents, alerts, dns_baselines RESTART IDENTITY CASCADE"
            )
        )
    yield make_sessionmaker(engine)
