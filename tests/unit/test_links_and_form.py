"""Checks 7 and 8: broken links, and contact-form health."""

import httpx
import pytest
import respx

from sitewatch.checks import form, links
from sitewatch.checks.form import find_forms, looks_like_a_contact_form
from sitewatch.checks.links import extract_links, same_host

SITE = "https://example.ca/"
CONTACT_HTML = """
<h1>Contact</h1>
<form action="https://formspree.io/f/abc123" method="post">
  <input type="text" name="name">
  <input type="email" name="email">
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
"""


def page(*hrefs: str, extra: str = "") -> str:
    body = "".join(f'<a href="{href}">link</a>' for href in hrefs)
    return f"<html><body>{body}{extra}</body></html>"


# --- link extraction ------------------------------------------------------------


def test_extract_links_makes_absolute_urls_and_skips_non_pages():
    html = page(
        "/about",
        "contact",
        "https://other.test/x",
        "mailto:hi@example.ca",
        "tel:+12505551234",
        "#top",
        "javascript:void(0)",
    )
    assert extract_links(html, SITE) == [
        "https://example.ca/about",
        "https://example.ca/contact",
        "https://other.test/x",
    ]


def test_extract_links_drops_fragments_and_duplicates():
    html = page("/about#team", "/about", "/about#history")
    assert extract_links(html, SITE) == ["https://example.ca/about"]


def test_extract_links_unescapes_entities():
    html = '<a href="/search?a=1&amp;b=2">x</a>'
    assert extract_links(html, SITE) == ["https://example.ca/search?a=1&b=2"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.ca/x", True),
        ("https://www.example.ca/x", True),
        ("http://example.ca/x", True),
        ("https://other.test/x", False),
        ("https://notexample.ca/x", False),
    ],
)
def test_same_host(url, expected):
    assert same_host(url, "example.ca") is expected


# --- the crawl -------------------------------------------------------------------


@respx.mock
async def test_all_links_fine_is_ok(make_clients):
    respx.get(SITE).respond(200, html=page("/about", "https://partner.test/"))
    respx.get("https://example.ca/about").respond(200, html=page("/"))
    respx.head("https://example.ca/about").respond(200)
    respx.head(SITE).respond(200)
    respx.head("https://partner.test/").respond(200)

    result = await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert result.status == "ok"
    assert result.detail["internal_broken"] == []
    assert "none broken" in result.summary


@respx.mock
async def test_broken_internal_link_fails(make_clients):
    respx.get(SITE).respond(200, html=page("/menu", "/about"))
    respx.head("https://example.ca/menu").respond(404)
    respx.head("https://example.ca/about").respond(200)
    respx.get("https://example.ca/about").respond(200, html=page())

    result = await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert result.status == "fail"
    [broken] = result.detail["internal_broken"]
    assert broken["url"] == "https://example.ca/menu"
    assert broken["status"] == 404
    assert broken["found_on"] == SITE


@respx.mock
async def test_broken_external_link_is_only_a_warning(make_clients):
    """Someone else's server going down must not page Owen."""
    respx.get(SITE).respond(200, html=page("https://partner.test/gone"))
    respx.head("https://partner.test/gone").respond(410)

    result = await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert result.status == "warn"
    assert result.detail["external_broken"][0]["status"] == 410


@respx.mock
async def test_head_refused_falls_back_to_get(make_clients):
    respx.get(SITE).respond(200, html=page("/about"))
    respx.head("https://example.ca/about").respond(405)
    respx.get("https://example.ca/about").respond(200, html=page())

    result = await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert result.status == "ok"


@respx.mock
async def test_crawl_respects_the_page_limit(make_clients):
    respx.get(SITE).respond(200, html=page(*[f"/p{i}" for i in range(10)]))
    for i in range(10):
        respx.head(f"https://example.ca/p{i}").respond(200)
        respx.get(f"https://example.ca/p{i}").respond(200, html=page("/"))
    respx.head(SITE).respond(200)

    result = await links.run({"domain": "example.ca", "url": SITE, "max_pages": 3}, make_clients())
    assert result.detail["pages_crawled"] <= 3


@respx.mock
async def test_each_link_is_only_checked_once(make_clients):
    route = respx.head("https://example.ca/about").respond(200)
    respx.get(SITE).respond(200, html=page("/about", "/about", "/other"))
    respx.head("https://example.ca/other").respond(200)
    respx.get("https://example.ca/about").respond(200, html=page("/about"))
    respx.get("https://example.ca/other").respond(200, html=page("/about"))

    await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert route.call_count == 1


@respx.mock
async def test_crawl_pauses_between_requests(make_clients, sleeps):
    respx.get(SITE).respond(200, html=page("/a", "/b"))
    respx.head("https://example.ca/a").respond(200)
    respx.head("https://example.ca/b").respond(200)
    respx.get("https://example.ca/a").respond(200, html=page())
    respx.get("https://example.ca/b").respond(200, html=page())

    await links.run({"domain": "example.ca", "url": SITE}, make_clients())
    assert sleeps.calls and all(pause <= 1 for pause in sleeps.calls)


@respx.mock
async def test_site_down_gives_no_verdict(make_clients):
    respx.get(SITE).respond(503)
    assert await links.run({"domain": "example.ca", "url": SITE}, make_clients()) is None


# --- contact form ----------------------------------------------------------------


def test_find_forms_reads_action_method_and_fields():
    [found] = find_forms(CONTACT_HTML)
    assert found["action"] == "https://formspree.io/f/abc123"
    assert found["method"] == "post"
    assert found["fields"] == ["name", "email", "message"]
    assert found["has_textarea"] is True


def test_a_search_box_is_not_a_contact_form():
    [search] = find_forms('<form action="/search"><input name="q"></form>')
    assert looks_like_a_contact_form(search) is False


def test_a_form_with_a_message_and_an_address_is_a_contact_form():
    assert looks_like_a_contact_form(find_forms(CONTACT_HTML)[0]) is True


@respx.mock
async def test_form_and_endpoint_healthy(make_clients):
    respx.get("https://example.ca/contact").respond(200, html=CONTACT_HTML)
    endpoint = respx.options("https://formspree.io/f/abc123").respond(200)

    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "ok"
    assert result.detail["endpoint"] == "https://formspree.io/f/abc123"
    assert endpoint.called


@respx.mock
async def test_the_form_is_never_submitted(make_clients):
    """The one thing this check must never do is email the client."""
    respx.get("https://example.ca/contact").respond(200, html=CONTACT_HTML)
    posted = respx.post("https://formspree.io/f/abc123").respond(200)
    respx.options("https://formspree.io/f/abc123").respond(204)

    await form.run({"domain": "example.ca"}, make_clients())
    assert posted.call_count == 0


@respx.mock
async def test_missing_form_fails(make_clients):
    respx.get("https://example.ca/contact").respond(200, html="<h1>Contact</h1><p>Call us.</p>")
    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "fail"
    assert "no contact form" in result.summary


@respx.mock
async def test_contact_page_404_fails(make_clients):
    respx.get("https://example.ca/contact").respond(404)
    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "fail"
    assert "HTTP 404" in result.summary


@respx.mock
async def test_dead_endpoint_fails(make_clients):
    respx.get("https://example.ca/contact").respond(200, html=CONTACT_HTML)
    respx.options("https://formspree.io/f/abc123").respond(404)
    respx.head("https://formspree.io/f/abc123").respond(404)
    respx.get("https://formspree.io/f/abc123").respond(404)

    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "fail"
    assert "returns HTTP 404" in result.summary


@respx.mock
async def test_endpoint_that_only_accepts_post_is_fine(make_clients):
    """405 from every safe method still proves the route exists."""
    respx.get("https://example.ca/contact").respond(200, html=CONTACT_HTML)
    respx.options("https://formspree.io/f/abc123").respond(405)
    respx.head("https://formspree.io/f/abc123").respond(405)
    respx.get("https://formspree.io/f/abc123").respond(405)

    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "ok"


@respx.mock
async def test_unreachable_endpoint_fails(make_clients):
    respx.get("https://example.ca/contact").respond(200, html=CONTACT_HTML)
    respx.options("https://formspree.io/f/abc123").mock(side_effect=httpx.ConnectError("no route"))

    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "fail"
    assert "unreachable" in result.summary


@respx.mock
async def test_form_with_no_action_is_ok(make_clients):
    html = CONTACT_HTML.replace(' action="https://formspree.io/f/abc123"', "")
    respx.get("https://example.ca/contact").respond(200, html=html)

    result = await form.run({"domain": "example.ca"}, make_clients())
    assert result.status == "ok"
    assert result.detail["endpoint"] is None


@respx.mock
async def test_a_custom_contact_url_is_used(make_clients):
    respx.get("https://example.ca/get-in-touch").respond(200, html=CONTACT_HTML)
    respx.options("https://formspree.io/f/abc123").respond(200)

    result = await form.run(
        {"domain": "example.ca", "contact_url": "https://example.ca/get-in-touch"}, make_clients()
    )
    assert result.status == "ok"
