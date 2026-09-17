"""Command line: `sitewatch <command>`.

sitewatch migrate              apply database migrations (alembic upgrade head)
sitewatch seed [sites.yaml]    load the site list into the database
sitewatch worker               run the scheduler until stopped
sitewatch api                  run the API and dashboard (uvicorn)
sitewatch check <domain> ...   run checks 1-4 once and print the results (no database)
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import httpx

from sitewatch.checks import REGISTRY, Clients
from sitewatch.checks.http import PageFetcher
from sitewatch.config import get_settings
from sitewatch.observability.logging import configure_logging


def _alembic_config() -> "object":
    from alembic.config import Config

    ini = Path(os.environ.get("SITEWATCH_ALEMBIC_INI", "alembic.ini"))
    if not ini.is_file():
        sys.exit(f"alembic.ini not found at {ini.resolve()} (set SITEWATCH_ALEMBIC_INI)")
    return Config(str(ini))


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command

    command.upgrade(_alembic_config(), "head")  # type: ignore[arg-type]


async def _seed(path: Path) -> None:
    from sitewatch.db.session import make_engine, make_sessionmaker
    from sitewatch.sites import load_sites_file, seed

    sites_file = load_sites_file(path)
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            report = await seed(session, sites_file)
    finally:
        await engine.dispose()
    print(json.dumps({"event": "seeded", "file": str(path), **asdict(report)}))


def cmd_seed(args: argparse.Namespace) -> None:
    asyncio.run(_seed(Path(args.path)))


def cmd_worker(_: argparse.Namespace) -> None:
    from sitewatch.worker.scheduler import Worker

    asyncio.run(Worker(get_settings()).run())


def cmd_api(_: argparse.Namespace) -> None:
    from sitewatch.api.app import run

    run()


async def _check(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}) as http:
        clients = Clients(
            http=http,
            pages=PageFetcher(http),
            uptime_retry_delay_seconds=args.retry_delay,
            rdap_base_url=settings.rdap_base_url,
        )
        url = args.url or f"https://{args.domain}/"
        config = {"domain": args.domain, "url": url, "expected_text": args.expected}
        worst = 0
        for kind in args.kinds:
            result = await REGISTRY[kind](config, clients)
            line = {"check": kind, "domain": args.domain}
            line |= (
                asdict(result) if result else {"status": None, "summary": "no verdict (skipped)"}
            )
            print(json.dumps(line, default=str))
            if result and result.status != "ok":
                worst = 1
        return worst


def cmd_check(args: argparse.Namespace) -> None:
    sys.exit(asyncio.run(_check(args)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sitewatch", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    p_seed = sub.add_parser("seed", help="load sites.yaml into the database")
    p_seed.add_argument("path", nargs="?", default="sites.yaml")
    p_seed.set_defaults(func=cmd_seed)

    sub.add_parser("worker", help="run the check scheduler").set_defaults(func=cmd_worker)
    sub.add_parser("api", help="run the API and dashboard").set_defaults(func=cmd_api)

    p_check = sub.add_parser("check", help="run checks once against a domain, no database")
    p_check.add_argument("domain")
    p_check.add_argument("--expected", help="text the page must contain (content check)")
    p_check.add_argument("--url", help="page to fetch (default https://<domain>/)")
    p_check.add_argument(
        "--kinds",
        nargs="+",
        default=["uptime", "content", "tls", "domain"],
        choices=sorted(REGISTRY),
    )
    p_check.add_argument("--retry-delay", type=float, default=2.0, help="uptime retry pause (s)")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if args.command != "check":
        configure_logging(get_settings().log_level)
    args.func(args)


if __name__ == "__main__":
    main()
