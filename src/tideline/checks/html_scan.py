"""Links and forms out of a page, safely.

The link and form checks used regular expressions to find `<a>` and `<form>`
tags. A security review (2026-09-18) measured those patterns slowing with the
square of the input on crafted HTML: a hacked client page of 3 MB of `<form `
would have kept the run busy for hours, and CPU work like that cannot be
interrupted by a timeout. Python's own HTML parser reads a page once, start to
end, so its time grows only with the page's size, and it copes with the broken
markup real sites are full of.

Only the first MAX_SCAN_BYTES of a page are read: links and forms live near the
top, and nothing a check needs is in the tail of a multi-megabyte page.
"""

from dataclasses import dataclass, field
from html.parser import HTMLParser

MAX_SCAN_BYTES = 1_000_000


@dataclass
class Anchor:
    href: str
    text: str = ""


@dataclass
class Form:
    action: str
    method: str
    fields: list[str] = field(default_factory=list)
    has_textarea: bool = False


@dataclass
class Scan:
    anchors: list[Anchor] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scan = Scan()
        self._anchor: Anchor | None = None
        self._form: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "").strip() for name, value in attrs}
        if tag == "a" and values.get("href"):
            self._anchor = Anchor(values["href"])
            self.scan.anchors.append(self._anchor)
        elif tag == "form":
            self._form = Form(values.get("action", ""), (values.get("method") or "get").lower())
            self.scan.forms.append(self._form)
        elif tag in ("input", "textarea", "select") and self._form is not None:
            if values.get("name"):
                self._form.fields.append(values["name"])
            if tag == "textarea":
                self._form.has_textarea = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._anchor = None
        elif tag == "form":
            self._form = None

    def handle_data(self, data: str) -> None:
        if self._anchor is not None and len(self._anchor.text) < 200:
            self._anchor.text += data


def scan(html: str) -> Scan:
    """Every link (with its text) and every form (with its fields) in a page."""
    scanner = _Scanner()
    scanner.feed(html[:MAX_SCAN_BYTES])
    scanner.close()
    return scanner.scan
