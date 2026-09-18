"""AWS Lambda entry points: the scheduled run, and the dashboard.

One container image, two functions (infra/lambda.tf):

- `run_handler`: started by EventBridge Scheduler on the 1st and 15th. Downloads
  the database from S3, migrates it, loads the site list, checks every site,
  uploads the database back. It can also be invoked by hand with a task:
  {"task": "run"} (the default; "kind" and "site" narrow it, e.g.
  {"task": "run", "kind": "dns", "site": "davesbakery.ca"}), {"task": "migrate"},
  or {"task": "report", "month": "2026-09"} to resend a month's reports.
- `web_handler`: the dashboard and API, behind CloudFront. Mangum translates
  each Lambda request into an ordinary ASGI request for the FastAPI app.

Secrets (API token, dashboard login, site list) are read from SSM Parameter
Store when a function starts, never stored in its configuration.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import boto3

from tideline.config import Settings, get_settings
from tideline.db.store import S3Database, StaleCopy
from tideline.observability.logging import configure_logging, log_event

log = logging.getLogger("tideline.lambda")

# SSM parameter name (under the prefix) -> the setting it fills.
SECRETS = {
    "api_token": "TIDELINE_API_TOKEN",
    "dashboard_user": "TIDELINE_DASHBOARD_USER",
    "dashboard_password": "TIDELINE_DASHBOARD_PASSWORD",
}
SITES_PARAMETER = "sites_yaml"
SITES_PATH = Path("/tmp/sites.yaml")


def load_secrets(prefix: str, region: str, with_sites: bool = False) -> None:
    """Put the secrets into this process's environment, where Settings reads them."""
    names = list(SECRETS) + ([SITES_PARAMETER] if with_sites else [])
    ssm = boto3.client("ssm", region_name=region)
    response = ssm.get_parameters(Names=[prefix + n for n in names], WithDecryption=True)
    found = {p["Name"].removeprefix(prefix): p["Value"] for p in response["Parameters"]}
    if response.get("InvalidParameters"):
        raise RuntimeError(f"missing SSM parameters: {response['InvalidParameters']}")
    for name, env in SECRETS.items():
        os.environ[env] = found[name]
    if with_sites:
        SITES_PATH.write_text(found[SITES_PARAMETER], encoding="utf-8")
    get_settings.cache_clear()


def _store(settings: Settings) -> S3Database:
    path = Path(settings.database_url.split("///", 1)[1])
    return S3Database(settings.db_bucket, settings.db_key, path, settings.aws_region)


# --- the run ---------------------------------------------------------------------


def run_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    task = event.get("task", "run")
    settings = get_settings()
    configure_logging(settings.log_level)
    load_secrets(settings.secrets_prefix, settings.aws_region, with_sites=task == "run")
    settings = get_settings()

    store = _store(settings)
    store.download()
    _migrate()

    result: dict[str, Any] = {"task": task}
    if task == "run":
        from dataclasses import asdict

        from tideline.worker.run import run

        report = asyncio.run(
            run(settings, sites_file=SITES_PATH, kind=event.get("kind"), domain=event.get("site"))
        )
        result |= asdict(report)
    elif task == "report":
        result["sent"] = asyncio.run(_send_reports(settings, str(event["month"])))
    elif task != "migrate":
        raise ValueError(f"unknown task {task!r}")

    try:
        store.upload()
    except StaleCopy:
        # Someone changed the database mid-run (the dashboard's DNS button).
        # Fail loudly: the Lambda error alarm emails Owen, and the next run
        # starts from the file as it is in S3.
        log.exception("database_upload_refused")
        raise
    log_event(log, "lambda_task_done", **result)
    return result


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(os.environ.get("TIDELINE_ALEMBIC_INI", "alembic.ini"))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


async def _send_reports(settings: Settings, month: str) -> int:
    from tideline.db.session import make_engine, make_sessionmaker
    from tideline.notify import build_notifier
    from tideline.reports.monthly import send_month

    year, number = (int(part) for part in month.split("-"))
    engine = make_engine(settings.database_url)
    try:
        return await send_month(make_sessionmaker(engine), build_notifier(settings), year, number)
    finally:
        await engine.dispose()


# --- the dashboard -------------------------------------------------------------------

_web: dict[str, Any] = {}


def _web_app() -> tuple[Any, S3Database]:
    """Built once per Lambda container, on its first request."""
    if not _web:
        from mangum import Mangum

        from tideline.api.app import create_app

        settings = get_settings()
        configure_logging(settings.log_level)
        load_secrets(settings.secrets_prefix, settings.aws_region)
        settings = get_settings()
        _web["handler"] = Mangum(create_app(settings), lifespan="auto")
        _web["store"] = _store(settings)
    return _web["handler"], _web["store"]


def _fingerprint(path: Path) -> tuple[int, int] | None:
    """Size and modification time: enough to tell whether SQLite wrote to the file."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def web_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    handler, store = _web_app()
    store.download()  # only transfers anything when the file changed
    before = _fingerprint(store.path)
    response: dict[str, Any] = handler(event, context)

    if _fingerprint(store.path) != before:
        # The request wrote to the database (accepting a DNS change): save it back.
        try:
            store.upload()
        except StaleCopy:
            log.exception("database_upload_refused")
            store.etag = None
            store.download()  # drop the local change; the page is reloaded fresh
            return {
                "statusCode": 409,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"detail": "a check run finished meanwhile; try again"}),
            }
    return response
