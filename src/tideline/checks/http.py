"""Fetch a page once and share it between the uptime and content checks.

Both checks run in the same run against the same URL. README section 2 says the
content check uses "the same request as #1", so the fetcher remembers each
page for a short time: whichever check runs first makes the request, and the
other reuses it. The uptime retry asks for `fresh=True` to bypass that memory.
"""

import asyncio
import time
from dataclasses import dataclass

import httpx

# README section 2: a request slower than 10 s counts as a failure.
REQUEST_TIMEOUT_SECONDS = 10.0
# Enough for any real homepage; stops a misbehaving server streaming forever.
MAX_BODY_BYTES = 3_000_000
CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class Page:
    url: str
    status_code: int | None
    final_url: str
    ttfb_ms: int | None
    total_ms: int
    body: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        # Redirects are followed, so the final status decides. 2xx and 3xx are up.
        return self.error is None and self.status_code is not None and self.status_code < 400


async def fetch_page(client: httpx.AsyncClient, url: str) -> Page:
    """GET a URL, recording status, time to first byte and total time.

    Never raises for network problems: they come back as `Page.error`.
    """
    started = time.perf_counter()

    def elapsed_ms() -> int:
        return round((time.perf_counter() - started) * 1000)

    ttfb_ms: int | None = None
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            # stream() returns as soon as the status line and headers arrive,
            # which is the time to first byte. Reading the body finishes the request.
            async with client.stream("GET", url, follow_redirects=True) as response:
                ttfb_ms = elapsed_ms()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_BODY_BYTES:
                        break
                raw = b"".join(chunks)[:MAX_BODY_BYTES]
                body = raw.decode(response.encoding or "utf-8", errors="replace")
                return Page(
                    url=url,
                    status_code=response.status_code,
                    final_url=str(response.url),
                    ttfb_ms=ttfb_ms,
                    total_ms=elapsed_ms(),
                    body=body,
                    error=None,
                )
    except TimeoutError:
        error = f"timed out after {REQUEST_TIMEOUT_SECONDS:g} s"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}".strip().rstrip(":") or type(exc).__name__
    return Page(
        url=url,
        status_code=None,
        final_url=url,
        ttfb_ms=ttfb_ms,
        total_ms=elapsed_ms(),
        body=None,
        error=error[:300],
    )


class PageFetcher:
    """Memoises fetch_page per URL for CACHE_SECONDS. Concurrent callers share one request."""

    def __init__(self, client: httpx.AsyncClient, cache_seconds: float = CACHE_SECONDS) -> None:
        self._client = client
        self._cache_seconds = cache_seconds
        self._entries: dict[str, tuple[float, asyncio.Task[Page]]] = {}

    async def fetch(self, url: str, fresh: bool = False) -> Page:
        now = time.monotonic()
        entry = self._entries.get(url)
        if fresh or entry is None or now - entry[0] > self._cache_seconds:
            task = asyncio.ensure_future(fetch_page(self._client, url))
            self._entries[url] = (now, task)
            entry = self._entries[url]
        return await asyncio.shield(entry[1])
