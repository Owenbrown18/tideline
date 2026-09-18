"""Check 2: is it still the client's site, and has it been hacked?

Two questions about the page body the uptime check already fetched:

1. Does it still contain the expected text (the business name)? A blank deploy,
   a hosting "account suspended" page or a parked domain all return HTTP 200,
   so status codes alone miss them.
2. Does it contain spam markers? Hacked small-business sites (Figs & Honey's old
   WordPress was one) get hidden casino and pharmacy links injected, often in
   markup a visitor never sees, so the raw HTML is searched, not just visible text.

The spam patterns are deliberately phrases, not single words: a musician's site
can mention playing a casino, so "casino" alone would be a false alarm, while
"online casino" or "slot gacor" on a bakery's site never is. A site can list
patterns to ignore in its config (`spam_ignore`) if a real one ever collides.
"""

import html
import re

from sitewatch.checks.base import Clients, Config, Result

SPAM_PATTERNS: dict[str, str] = {
    "viagra": r"\bviagra\b",
    "cialis": r"\bcialis\b",
    "sildenafil": r"\bsildenafil\b",
    "tadalafil": r"\btadalafil\b",
    "online pharmacy": r"\b(?:online|canadian|mexican)[\s-]+pharmacy\b",
    "no prescription": r"\bwithout\s+(?:a\s+)?prescription\b|\bno\s+prescription\s+needed\b",
    "online casino": r"\b(?:online|live|crypto)[\s-]+casinos?\b|\bcasino\s+(?:bonus|online)\b",
    "slot gambling": r"\bslot\s+(?:gacor|online|88)\b|\bslot88\b",
    "judi/togel": r"\b(?:situs\s+judi|judi\s+online|togel|sbobet)\b",
    "payday loans": r"\bpayday\s+loans?\b",
    "replica watches": r"\breplica\s+(?:watches|rolex|handbags)\b",
    "essay mill": r"\b(?:buy|cheap)\s+essays?\s+(?:online|writing)\b",
    "porn": r"\bporn(?:o|hub)?\b|\bxxx\s+videos?\b",
}
_COMPILED = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in SPAM_PATTERNS.items()}

# Curly quotes and dashes that CMSes substitute for the plain ones people type.
_TYPOGRAPHIC = str.maketrans(
    {
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote (the usual apostrophe substitute)
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # non-breaking space
    }
)


def normalise(text: str) -> str:
    """Make "Dave&#8217;s  Bakery" and "Dave's Bakery" compare equal."""
    text = html.unescape(text).translate(_TYPOGRAPHIC)
    return re.sub(r"\s+", " ", text).casefold()


def find_spam(body: str, ignore: list[str] | None = None) -> list[dict[str, str]]:
    skipped = set(ignore or [])
    text = html.unescape(body)
    matches = []
    for name, pattern in _COMPILED.items():
        if name in skipped:
            continue
        found = pattern.search(text)
        if found:
            start, end = max(found.start() - 60, 0), min(found.end() + 60, len(text))
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            matches.append({"marker": name, "snippet": snippet})
    return matches


async def run(config: Config, clients: Clients) -> Result | None:
    url: str = config["url"]
    page = await clients.pages.fetch(url)
    if not page.ok or page.body is None:
        return None  # the uptime check reports this; nothing to judge here

    expected: str | None = config.get("expected_text")
    expected_found = expected is None or normalise(expected) in normalise(page.body)
    spam = find_spam(page.body, config.get("spam_ignore"))
    detail = {
        "url": url,
        "expected_text": expected,
        "expected_text_found": expected_found,
        "spam_matches": spam,
        "body_bytes": len(page.body),
    }

    problems = []
    if spam:
        problems.append("Spam found on the page: " + ", ".join(m["marker"] for m in spam))
    if not expected_found:
        problems.append(f'The page no longer shows "{expected}"')
    if problems:
        return Result("fail", ". ".join(problems), detail)
    return Result("ok", "Business name present, no spam", detail)
