"""Check 1: is the page up, and how fast is it?

One failure is retried once after a pause (30 s in production) before it counts,
so a single dropped packet never becomes a result of "fail". Opening an
incident needs 2 failed results in a row on top of that (see incidents/engine.py).
"""

from typing import Any

from tideline.checks.base import Clients, Config, Result
from tideline.checks.http import Page


def _describe(page: Page) -> str:
    if page.error:
        return page.error
    return f"HTTP {page.status_code}"


def _detail(page: Page) -> dict[str, Any]:
    return {
        "url": page.url,
        "final_url": page.final_url,
        "status_code": page.status_code,
        "ttfb_ms": page.ttfb_ms,
        "total_ms": page.total_ms,
        "error": page.error,
    }


async def run(config: Config, clients: Clients) -> Result:
    url: str = config["url"]
    first = await clients.pages.fetch(url)
    if first.ok:
        return Result("ok", f"HTTP {first.status_code} in {first.total_ms} ms", _detail(first))

    await clients.sleep(clients.uptime_retry_delay_seconds)
    retry = await clients.pages.fetch(url, fresh=True)
    if retry.ok:
        detail = _detail(retry) | {"retried": True, "first_attempt": _describe(first)}
        summary = f"HTTP {retry.status_code} in {retry.total_ms} ms, after one retry"
        return Result("ok", summary, detail)

    detail = _detail(retry) | {"retried": True, "first_attempt": _describe(first)}
    return Result("fail", f"The site is down ({_describe(retry)})", detail)
