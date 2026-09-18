"""Integration tests run against a real SQLite database file, the same engine
production uses. Nothing to install or start: each test session builds a fresh
file in a temporary directory through the real migrations, and every table is
emptied between tests.
"""

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tideline.db.models import Base
from tideline.db.session import make_engine, make_sessionmaker

REPO = Path(__file__).resolve().parents[2]
_DIR = tempfile.mkdtemp(prefix="tideline-tests-")
DATABASE_URL = os.environ.get("TIDELINE_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_DIR}/test.db")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


def alembic_config() -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
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
        # Children first. SQLite reuses ids once a table is empty, so every
        # test's first site is id 1 again.
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(text(f"DELETE FROM {table.name}"))
    yield make_sessionmaker(engine)
