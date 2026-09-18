"""The HTML scanner behind the link and form checks (checks/html_scan.py)."""

import time

import pytest

from tideline.checks.form import contact_page_candidates, find_forms
from tideline.checks.html_scan import scan
from tideline.checks.links import extract_links


def test_it_finds_links_with_their_text_and_forms_with_their_fields():
    page = scan(
        "<nav><a href='/contact'>Get <b>in</b> touch</a></nav>"
        "<form action='https://formspree.io/f/x' method='POST'>"
        "<input name='email'><textarea name='message'></textarea></form>"
    )
    assert [(a.href, " ".join(a.text.split())) for a in page.anchors] == [
        ("/contact", "Get in touch")
    ]
    [form] = page.forms
    assert (form.action, form.method, form.fields, form.has_textarea) == (
        "https://formspree.io/f/x",
        "post",
        ["email", "message"],
        True,
    )


def test_broken_markup_does_not_confuse_it():
    page = scan("<a href='/a'>one<a href='/b'>two</a><form><input name=q>")
    assert [a.href for a in page.anchors] == ["/a", "/b"]
    assert page.forms[0].fields == ["q"]


@pytest.mark.parametrize(
    "hostile",
    [
        "<form " + ">" * 3_000_000,  # measured at hours with the old pattern
        "<a href='x'>c" * 250_000,  # measured at about 1,600 s with the old pattern
        "<a " * 1_000_000,
    ],
)
def test_a_hostile_page_cannot_stall_a_run(hostile):
    started = time.perf_counter()
    extract_links(hostile, "https://example.ca/")
    find_forms(hostile)
    contact_page_candidates(hostile, "https://example.ca/")
    assert time.perf_counter() - started < 5
