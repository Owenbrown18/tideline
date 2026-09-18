"""Check 7: broken links.

Crawls the site's own pages (same host only, to a depth and page limit) and
checks every link found. Internal links that return 4xx or 5xx are a failure:
they are the client's own pages, and a 404 in a menu is the kind of thing a
visitor hits and nobody notices for months.

External links are checked too, but a broken one is a **warning**, not a
failure: the other end is somebody else's server, it may block robots, and
Tideline should not page Owen because a supplier's site is down.

Politeness, because this crawls real client sites:
- one host, the client's own, and never more than `max_pages` pages;
- a small pause between requests;
- HEAD first for external links, falling back to GET when HEAD is refused;
- the honest Tideline user agent, as everywhere else.
"""

import re
from collections import deque
from html import unescape
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from tideline.brand import short_url
from tideline.checks.base import Clients, Config, Result

# What counts as broken.
#
# Internal: any 4xx or 5xx, because it is the client's own page. 429 is excluded
# because that is their own CDN rate-limiting the crawl, not a broken page.
#
# External: only "gone" answers and connection failures. Measured on the live
# sites (2026-09-17): Instagram replies 429 to anything without a browser
# session, and LinkedIn replies 999. Those links work perfectly for visitors, so
# reporting them would train Owen to ignore this check.
INTERNAL_OK_STATUSES = (429,)
EXTERNAL_BROKEN_STATUSES = (404, 410)

MAX_PAGES = 25
MAX_DEPTH = 2
PAUSE_SECONDS = 0.2
LINK_TIMEOUT = 10.0

HREF = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
# Links that are not fetchable pages.
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "#")


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute, fragment-free URLs from the anchors in a page."""
    links = []
    for raw in HREF.findall(html):
        href = unescape(raw.strip())
        if not href or href.lower().startswith(SKIP_SCHEMES):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)
    return list(dict.fromkeys(links))  # de-duplicated, order kept


def same_host(url: str, host: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    host = host.lower()
    return netloc in (host, f"www.{host}") or netloc.removeprefix("www.") == host.removeprefix(
        "www."
    )


async def _status_of(client: httpx.AsyncClient, url: str) -> tuple[int | None, str | None]:
    """(status, error), using HEAD where possible.

    HEAD is cheap and polite, but plenty of servers handle it badly: some answer
    405 or 501, and Square's checkout links answer 404 to HEAD and 200 to GET
    (measured 2026-09-17, after Tideline reported a working "buy" button as
    broken). So any answer that would be reported as a problem is confirmed with
    a real GET before it counts.
    """
    try:
        response = await client.head(url, follow_redirects=True, timeout=LINK_TIMEOUT)
    except httpx.HTTPError:
        response = None

    if response is not None and response.status_code < 400:
        return response.status_code, None

    try:
        confirm = await client.get(url, follow_redirects=True, timeout=LINK_TIMEOUT)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    return confirm.status_code, None


def describe_broken(broken: list[dict[str, Any]], host: str, internal: bool) -> str:
    """One broken link: "Broken link: /testimonials returns 404".
    Several: "3 broken links: /a (404), /b (404) and 1 more"."""

    def what(b: dict[str, Any]) -> str:
        return str(b["status"]) if b["status"] is not None else "no answer"

    if len(broken) == 1:
        b = broken[0]
        verb = f"returns {what(b)}" if b["status"] is not None else "does not answer"
        where = "" if internal else " to another site"
        return f"Broken link{where}: {short_url(b['url'], host)} {verb}"
    where = "" if internal else " to other sites"
    first = ", ".join(f"{short_url(b['url'], host)} ({what(b)})" for b in broken[:3])
    extra = f" and {len(broken) - 3} more" if len(broken) > 3 else ""
    return f"{len(broken)} broken links{where}: {first}{extra}"


async def run(config: Config, clients: Clients) -> Result | None:
    start_url: str = config.get("url") or f"https://{config['domain']}/"
    host = urlparse(start_url).netloc or config["domain"]
    max_pages = int(config.get("max_pages", MAX_PAGES))
    max_depth = int(config.get("max_depth", MAX_DEPTH))

    first = await clients.pages.fetch(start_url)
    if not first.ok or first.body is None:
        return None  # the site being down is the uptime check's incident

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    crawled: dict[str, str] = {start_url: first.body}
    seen_pages = {start_url}
    checked: dict[str, tuple[int | None, str | None]] = {}
    reported: set[str] = set()
    internal_broken: list[dict[str, Any]] = []
    external_broken: list[dict[str, Any]] = []

    while queue and len(seen_pages) <= max_pages:
        page_url, depth = queue.popleft()
        body = crawled.get(page_url)
        if body is None:
            page = await clients.pages.fetch(page_url)
            if not page.ok or page.body is None:
                continue
            body = page.body

        for link in extract_links(body, page_url):
            internal = same_host(link, host)
            if link not in checked:
                await clients.sleep(PAUSE_SECONDS)
                checked[link] = await _status_of(clients.http, link)
            status, error = checked[link]
            if internal:
                broken = error is not None or (
                    status is not None and status >= 400 and status not in INTERNAL_OK_STATUSES
                )
            else:
                broken = error is not None or status in EXTERNAL_BROKEN_STATUSES

            if broken and link not in reported:
                # One entry per URL, not per page it appears on: a broken link in
                # a footer is one problem, not one problem per page.
                reported.add(link)
                record = {
                    "url": link,
                    "status": status,
                    "error": error,
                    "found_on": page_url,
                }
                (internal_broken if internal else external_broken).append(record)

            if (
                internal
                and depth < max_depth
                and link not in seen_pages
                and len(seen_pages) < max_pages
                and not broken
            ):
                seen_pages.add(link)
                queue.append((link, depth + 1))

    detail: dict[str, Any] = {
        "start_url": start_url,
        "pages_crawled": len(seen_pages),
        "links_checked": len(checked),
        "internal_broken": internal_broken,
        "external_broken": external_broken,
    }

    if internal_broken:
        return Result("fail", describe_broken(internal_broken, host, internal=True), detail)
    if external_broken:
        return Result("warn", describe_broken(external_broken, host, internal=False), detail)
    return Result(
        "ok",
        f"{len(checked)} links checked across {len(seen_pages)} pages, none broken",
        detail,
    )


__all__ = ["extract_links", "run", "same_host"]
