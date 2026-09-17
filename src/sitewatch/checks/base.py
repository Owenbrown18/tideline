"""What every check takes and returns.

A check is an async function `run(config, clients) -> Result | None`:

- `config` is a plain dict: the check's own settings merged over its site's
  (domain, name, expected_text). No database access inside a check.
- `clients` carries everything that touches the outside world (HTTP client,
  shared page fetcher, TLS context, clock, sleep), so tests can swap any of it.
- It returns a Result, or None when the check could not form an opinion (for
  example the content check when the page did not load: the uptime check owns
  that failure, so the content check stays quiet instead of double-reporting).
"""

import asyncio
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from sitewatch.checks.http import PageFetcher

Status = Literal["ok", "warn", "fail"]
Config = Mapping[str, Any]


@dataclass(frozen=True)
class Result:
    status: Status
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Clients:
    http: httpx.AsyncClient
    pages: PageFetcher
    ssl_context: ssl.SSLContext = field(default_factory=ssl.create_default_context)
    now: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    uptime_retry_delay_seconds: float = 30.0
    rdap_base_url: str = "https://rdap.org/domain/"


CheckFn = Callable[[Config, Clients], Awaitable[Result | None]]
