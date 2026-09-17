import asyncio

import httpx
import respx

from sitewatch.checks import uptime
from sitewatch.checks.http import PageFetcher, fetch_page

URL = "https://example.ca/"


@respx.mock
async def test_up_on_first_try(make_clients, sleeps):
    respx.get(URL).respond(200, text="<html>hello</html>")
    result = await uptime.run({"url": URL}, make_clients())
    assert result.status == "ok"
    assert result.detail["status_code"] == 200
    assert result.detail["ttfb_ms"] is not None
    assert result.summary.startswith("HTTP 200 in ")
    assert sleeps.calls == []  # no retry needed


@respx.mock
async def test_redirect_is_followed_and_final_status_counts(make_clients):
    respx.get(URL).respond(301, headers={"Location": "https://www.example.ca/"})
    respx.get("https://www.example.ca/").respond(200, text="ok")
    result = await uptime.run({"url": URL}, make_clients())
    assert result.status == "ok"
    assert result.detail["final_url"] == "https://www.example.ca/"


@respx.mock
async def test_one_failure_then_success_is_ok_after_retry(make_clients, sleeps):
    respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    result = await uptime.run({"url": URL}, make_clients())
    assert result.status == "ok"
    assert result.detail["retried"] is True
    assert result.detail["first_attempt"] == "HTTP 503"
    assert sleeps.calls == [30.0]  # README: one retry after 30 s


@respx.mock
async def test_two_failures_is_fail(make_clients, sleeps):
    route = respx.get(URL).respond(500)
    result = await uptime.run({"url": URL}, make_clients())
    assert result.status == "fail"
    assert route.call_count == 2
    assert "HTTP 500" in result.summary
    assert sleeps.calls == [30.0]


@respx.mock
async def test_connection_error_is_fail(make_clients):
    respx.get(URL).mock(side_effect=httpx.ConnectError("connection refused"))
    result = await uptime.run({"url": URL}, make_clients())
    assert result.status == "fail"
    assert "ConnectError" in result.detail["error"]


async def test_timeout_over_ten_seconds_is_an_error(monkeypatch, http):
    from sitewatch.checks import http as http_module

    monkeypatch.setattr(http_module, "REQUEST_TIMEOUT_SECONDS", 0.05)

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    with respx.mock:
        respx.get(URL).mock(side_effect=slow)
        page = await fetch_page(http, URL)
    assert not page.ok
    assert page.error.startswith("timed out")


@respx.mock
async def test_page_fetcher_shares_one_request_between_checks(http):
    route = respx.get(URL).respond(200, text="body")
    fetcher = PageFetcher(http, cache_seconds=60)
    first, second = await asyncio.gather(fetcher.fetch(URL), fetcher.fetch(URL))
    third = await fetcher.fetch(URL)
    assert first is second is third
    assert route.call_count == 1

    await fetcher.fetch(URL, fresh=True)
    assert route.call_count == 2


@respx.mock
async def test_body_is_capped(monkeypatch, http):
    from sitewatch.checks import http as http_module

    monkeypatch.setattr(http_module, "MAX_BODY_BYTES", 10)
    respx.get(URL).respond(200, content=b"x" * 1000)
    page = await fetch_page(http, URL)
    assert page.ok
    assert len(page.body) < 1000
