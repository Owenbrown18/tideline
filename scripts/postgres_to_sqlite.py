"""One-off: copy the Postgres database into a new SQLite file (September 2026).

Used once, when Tideline moved from Postgres on EC2 to SQLite on Lambda
(docs/decisions/0005). Kept because it documents how the move was done and can
be rerun from the final Postgres backup if ever needed.

    # restore the final backup into a local Postgres first, then:
    uv run python scripts/postgres_to_sqlite.py \
        postgresql+psycopg://sitewatch:sitewatch@localhost:5432/prod_copy tideline.db

Source tables are read with their own (reflected) Postgres types; rows are
written through the app's models, so the target gets exactly what the app
itself would write (JSON as JSON, times as naive UTC).
"""

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, func, insert, select

from tideline.db.models import Base

REPO = Path(__file__).resolve().parents[1]


def main(source_url: str, target_path: str) -> None:
    target = Path(target_path)
    if target.exists():
        sys.exit(f"{target} already exists; refusing to overwrite it")
    target_url = f"sqlite:///{target}"

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", target_url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")

    source_engine = create_engine(source_url)
    source = MetaData()
    source.reflect(source_engine)
    target_engine = create_engine(target_url)

    with source_engine.connect() as src, target_engine.begin() as dst:
        for table in Base.metadata.sorted_tables:  # parents before children
            rows = [dict(row._mapping) for row in src.execute(select(source.tables[table.name]))]
            if rows:
                dst.execute(insert(table), rows)
            copied = dst.execute(select(func.count()).select_from(table)).scalar_one()
            print(f"{table.name:15} {len(rows):6} read  {copied:6} written")
            if copied != len(rows):
                raise SystemExit(f"{table.name}: row counts differ")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
