import pytest
import respx

from tideline.checks import content
from tideline.checks.content import find_spam, normalise

URL = "https://example.ca/"


def config(**extra):
    return {"url": URL, "expected_text": "Daves' Bakery", **extra}


@respx.mock
async def test_expected_text_present_is_ok(make_clients):
    respx.get(URL).respond(200, html="<title>Daves' Bakery</title>")
    result = await content.run(config(), make_clients())
    assert result.status == "ok"
    assert result.detail["expected_text_found"] is True


@pytest.mark.parametrize(
    "markup",
    [
        "<title>Daves&#x27; Bakery</title>",  # entity-encoded apostrophe (Next.js does this)
        "<h1>DAVES’   BAKERY</h1>",  # curly apostrophe, capitals, extra spaces
        "<p>Daves&#8217;\nBakery</p>",
    ],
)
@respx.mock
async def test_expected_text_survives_html_typography(make_clients, markup):
    respx.get(URL).respond(200, html=markup)
    result = await content.run(config(), make_clients())
    assert result.status == "ok"


@respx.mock
async def test_missing_expected_text_fails(make_clients):
    respx.get(URL).respond(200, html="<h1>Account suspended</h1>")
    result = await content.run(config(), make_clients())
    assert result.status == "fail"
    assert "no longer shows" in result.summary


@respx.mock
async def test_hidden_spam_links_fail(make_clients):
    respx.get(URL).respond(
        200,
        html="<title>Daves' Bakery</title>"
        '<div style="display:none"><a href="//x">cheap viagra</a> <a>slot gacor</a></div>',
    )
    result = await content.run(config(), make_clients())
    assert result.status == "fail"
    markers = {m["marker"] for m in result.detail["spam_matches"]}
    assert markers == {"viagra", "slot gambling"}
    assert "Spam found on the page" in result.summary


@respx.mock
async def test_page_that_did_not_load_gives_no_verdict(make_clients):
    respx.get(URL).respond(502)
    assert await content.run(config(), make_clients()) is None


@respx.mock
async def test_no_expected_text_configured_only_checks_spam(make_clients):
    respx.get(URL).respond(200, html="<h1>anything</h1>")
    result = await content.run({"url": URL, "expected_text": None}, make_clients())
    assert result.status == "ok"


@pytest.mark.parametrize(
    "text",
    [
        "Suzanne plays the Casino Rama stage in May",  # a venue, not spam
        "Our pharmacy partners",
        "Loans of our tools are available",
        "prescription glasses cleaning",
    ],
)
def test_ordinary_words_are_not_spam(text):
    assert find_spam(text) == []


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("Buy Cialis today", "cialis"),
        ("best online casino bonus", "online casino"),
        ("Canadian pharmacy no prescription needed", "online pharmacy"),
        ("situs judi terpercaya", "judi/togel"),
        ("fast payday loans", "payday loans"),
        ("replica rolex", "replica watches"),
    ],
)
def test_spam_markers_are_found(text, marker):
    assert marker in {m["marker"] for m in find_spam(text)}


def test_spam_ignore_skips_a_marker():
    assert find_spam("payday loans", ignore=["payday loans"]) == []


def test_spam_snippet_gives_context():
    [match] = find_spam("x" * 200 + " buy viagra now " + "y" * 200)
    assert "buy viagra now" in match["snippet"]
    assert len(match["snippet"]) < 150


def test_normalise():
    assert normalise("Figs &amp; Honey") == normalise("figs & honey")
