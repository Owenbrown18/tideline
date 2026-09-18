"""Alembic environment: runs migrations with a plain (sync) SQLite connection.

The URL comes from `sqlalchemy.url` if a caller set it (the test suite does),
otherwise from Tideline settings (TIDELINE_DATABASE_URL).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from tideline.config import get_settings
from tideline.db.models import Base
from tideline.db.session import sync_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    return sync_url(config.get_main_option("sqlalchemy.url") or get_settings().database_url)


def run_migrations_offline() -> None:
    """Print SQL instead of running it: `alembic upgrade head --sql`."""
    context.configure(
        url=database_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # SQLite cannot ALTER most things in place; batch mode copies the table.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
