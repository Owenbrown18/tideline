"""Command line: `sitewatch <command>`.

sitewatch migrate              apply database migrations (alembic upgrade head)
sitewatch seed [sites.yaml]    load the site list into the database
sitewatch worker               run the scheduler until stopped
sitewatch api                  run the API and dashboard (uvicorn)
sitewatch check <domain> ...   run checks once and print the results (no database)
sitewatch run-once --kind dns  run every enabled check of a kind now, writing results
sitewatch rollup [--day]       summarise a day into daily_rollups, and purge old raw results
sitewatch report --month 2026-09 [--site domain] [--email]   monthly report(s)
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


async def _run_once(kind: str | None, domain: str | None) -> None:
    from sqlalchemy import select

    from sitewatch.db.models import Check, Site
    from sitewatch.db.session import make_engine, make_sessionmaker
    from sitewatch.worker.scheduler import Worker

    settings = get_settings()
    worker = Worker(settings)
    engine = make_engine(settings.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            query = select(Check.id).join(Site).where(Check.enabled, Site.active)
            if kind:
                query = query.where(Check.kind == kind)
            if domain:
                query = query.where(Site.domain == domain)
            check_ids = list(await session.scalars(query))
        for check_id in check_ids:
            await worker.runner.run_check(check_id)
        print(json.dumps({"event": "run_once", "checks": len(check_ids), "kind": kind}))
    finally:
        await engine.dispose()
        await worker.http.aclose()
        await worker.engine.dispose()


def cmd_run_once(args: argparse.Namespace) -> None:
    asyncio.run(_run_once(args.kind, args.site))


async def _rollup(day_text: str | None) -> None:
    from datetime import UTC, date, datetime, timedelta

    from sitewatch.db.session import make_engine, make_sessionmaker
    from sitewatch.reports.rollups import purge_old_results, rollup_day

    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            day = (
                date.fromisoformat(day_text)
                if day_text
                else (datetime.now(UTC) - timedelta(days=1)).date()
            )
            sites = await rollup_day(session, day)
            purged = await purge_old_results(session, datetime.now(UTC))
        print(
            json.dumps(
                {"event": "rollup", "day": day.isoformat(), "sites": sites, "purged": purged}
            )
        )
    finally:
        await engine.dispose()


def cmd_rollup(args: argparse.Namespace) -> None:
    asyncio.run(_rollup(args.day))


async def _report(month: str, domain: str | None, email: bool, out_dir: str | None) -> None:
    from sqlalchemy import select

    from sitewatch.db.models import Site
    from sitewatch.db.session import make_engine, make_sessionmaker
    from sitewatch.notify.ses import SesNotifier
    from sitewatch.reports.monthly import build_report, render_html, render_text

    year, month_number = (int(part) for part in month.split("-"))
    settings = get_settings()
    # Reports go to Owen only, the same single recipient as every alert.
    notifier = (
        SesNotifier(settings.aws_region, settings.alert_sender, settings.alert_email)
        if email
        else None
    )
    engine = make_engine(settings.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            query = select(Site.id).where(Site.active).order_by(Site.name)
            if domain:
                query = query.where(Site.domain == domain)
            site_ids = list(await session.scalars(query))
            for site_id in site_ids:
                report = await build_report(session, site_id, year, month_number)
                html = render_html(report)
                if out_dir:
                    path = Path(out_dir) / f"{report.domain}-{month}.html"
                    path.write_text(html, encoding="utf-8")
                    print(json.dumps({"event": "report_written", "file": str(path)}))
                if notifier is not None:
                    await notifier.send_report(report.subject, render_text(report), html)
                    print(json.dumps({"event": "report_emailed", "site": report.domain}))
                if not email and not out_dir:
                    print(render_text(report))
                    print()
    finally:
        await engine.dispose()


def cmd_report(args: argparse.Namespace) -> None:
    asyncio.run(_report(args.month, args.site, args.email, args.out_dir))


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

    p_run = sub.add_parser("run-once", help="run enabled checks now instead of waiting")
    p_run.add_argument("--kind", choices=sorted(REGISTRY), help="only this kind of check")
    p_run.add_argument("--site", help="only this domain")
    p_run.set_defaults(func=cmd_run_once)

    p_rollup = sub.add_parser("rollup", help="summarise a day and purge old raw results")
    p_rollup.add_argument("--day", help="YYYY-MM-DD (default: yesterday)")
    p_rollup.set_defaults(func=cmd_rollup)

    p_report = sub.add_parser("report", help="monthly report per site")
    p_report.add_argument("--month", required=True, help="YYYY-MM")
    p_report.add_argument("--site", help="one domain (default: every active site)")
    p_report.add_argument("--email", action="store_true", help="email it to Owen through SES")
    p_report.add_argument("--out-dir", help="write the HTML to this directory")
    p_report.set_defaults(func=cmd_report)

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
