"""Check 8: is the contact form still there, and can it still deliver?

A contact form that silently stops working costs a small business real money,
and nobody notices until someone complains they never heard back. Tideline
checks the two things that can be checked without sending anything:

1. the contact page still loads and still contains a form, with the fields a
   contact form needs (somewhere to type a message, somewhere for an address);
2. the endpoint that form posts to still exists and answers.

**It never submits the form.** That would email the client, which is the one
thing Tideline must never cause, so the endpoint is probed with a request that
creates nothing: OPTIONS, falling back to a HEAD or GET. Formspree, Web3Forms
and the like answer those without recording a submission. A form posting to the
site's own API route is treated the same way.
"""

import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from tideline.brand import short_url
from tideline.checks.base import Clients, Config, Result

FORM_TIMEOUT = 10.0

# How a contact page is usually linked. Checked against the URL path, then
# against the link text, in this order.
CONTACT_PATHS = ("/contact", "/contact-us", "/contactus", "/get-in-touch", "/enquire", "/booking")
CONTACT_WORDS = ("contact", "get in touch", "enquire", "enquiry", "book", "quote")

# Endpoint responses that mean "this exists": anything that is not a 404/410,
# and not a server error. Form backends answer OPTIONS or GET with 200, 204,
# 405 (method not allowed, but the route exists) or 422 (validation), all of
# which prove the endpoint is alive.
ENDPOINT_MISSING = (404, 410)


def contact_page_candidates(html: str, base_url: str) -> list[str]:
    """Links on a page that look like they lead to a contact page, best first.

    Assuming /contact was wrong on five of the eleven live sites (2026-09-17):
    some put the form on the home page, others use /booking or /enquire. The
    check now follows the site's own navigation instead.
    """
    host = urlparse(base_url).netloc
    anchors = re.findall(
        r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    by_path: list[str] = []
    by_text: list[str] = []
    for href, text in anchors:
        absolute = urljoin(base_url, href.strip())
        if urlparse(absolute).netloc != host:
            continue
        path = urlparse(absolute).path.rstrip("/").lower() or "/"
        words = re.sub(r"<[^>]+>", " ", text).strip().lower()
        if any(path.endswith(candidate) for candidate in CONTACT_PATHS):
            by_path.append(absolute.split("#")[0])
        elif any(word in words for word in CONTACT_WORDS):
            by_text.append(absolute.split("#")[0])
    ordered = by_path + by_text
    return list(dict.fromkeys(ordered))


def find_forms(html: str) -> list[dict[str, Any]]:
    """Every <form> in the page, with its action, method and field names."""
    forms = []
    for match in re.finditer(r"<form\b(.*?)>(.*?)</form>", html, re.IGNORECASE | re.DOTALL):
        attributes, body = match.group(1), match.group(2)

        def attribute(name: str, text: str = attributes) -> str:
            found = re.search(rf"""\b{name}\s*=\s*["']([^"']*)["']""", text, re.IGNORECASE)
            return found.group(1).strip() if found else ""

        fields = [
            f
            for f in re.findall(
                r"""<(?:input|textarea|select)\b[^>]*?\bname\s*=\s*["']([^"']+)["']""",
                body,
                re.IGNORECASE,
            )
        ]
        has_textarea = bool(re.search(r"<textarea\b", body, re.IGNORECASE))
        forms.append(
            {
                "action": attribute("action"),
                "method": (attribute("method") or "get").lower(),
                "fields": fields,
                "has_textarea": has_textarea,
            }
        )
    return forms


def looks_like_a_contact_form(form: dict[str, Any]) -> bool:
    """A search box is a form too. A contact form takes a message."""
    names = " ".join(form["fields"]).lower()
    wants_message = form["has_textarea"] or "message" in names or "enquiry" in names
    wants_contact = any(word in names for word in ("email", "mail", "phone", "tel", "name"))
    return bool(wants_message and wants_contact)


async def probe_endpoint(client: httpx.AsyncClient, url: str) -> tuple[int | None, str | None]:
    """Reach the endpoint without submitting anything."""
    for method in ("OPTIONS", "HEAD", "GET"):
        try:
            response = await client.request(
                method, url, follow_redirects=True, timeout=FORM_TIMEOUT
            )
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}"[:200]
        if response.status_code not in (405, 501):
            return response.status_code, None
    return 405, None  # every method refused, but the host answered: the route exists


async def run(config: Config, clients: Clients) -> Result | None:
    domain: str = config["domain"]
    configured: str | None = config.get("contact_url")
    home_url: str = config.get("url") or f"https://{domain}/"

    if configured:
        # Told where the form is: a missing page there is a real failure.
        page = await clients.pages.fetch(configured)
        if page.error is not None:
            return None
        if page.status_code is not None and page.status_code >= 400:
            return Result(
                "fail",
                f"The contact page {short_url(configured)} returns HTTP {page.status_code}",
                {"contact_url": configured, "status_code": page.status_code},
            )
        if page.body is None:
            return None
        searched = [configured]
        found_on, body = configured, page.body
        contact_forms = [f for f in find_forms(body) if looks_like_a_contact_form(f)]
        forms = find_forms(body)
    else:
        # Not told: follow the site's own navigation, starting at the home page,
        # which is also where single-page sites keep their form.
        home = await clients.pages.fetch(home_url)
        if not home.ok or home.body is None:
            return None
        searched = [home_url]
        found_on, body = home_url, home.body
        forms = find_forms(home.body)
        contact_forms = [f for f in forms if looks_like_a_contact_form(f)]

        if not contact_forms:
            for candidate in contact_page_candidates(home.body, home_url)[:3]:
                searched.append(candidate)
                candidate_page = await clients.pages.fetch(candidate)
                if not candidate_page.ok or candidate_page.body is None:
                    continue
                candidate_forms = find_forms(candidate_page.body)
                matches = [f for f in candidate_forms if looks_like_a_contact_form(f)]
                if matches:
                    found_on, body = candidate, candidate_page.body
                    forms, contact_forms = candidate_forms, matches
                    break

    page_url = found_on
    detail: dict[str, Any] = {
        "contact_url": page_url,
        "pages_searched": searched,
        "forms_found": len(forms),
        "contact_forms_found": len(contact_forms),
    }

    if not contact_forms:
        pages = "page" if len(searched) == 1 else "pages"
        where = short_url(configured) if configured else f"{len(searched)} {pages} searched"
        return Result(
            "fail",
            f"No contact form found ({where})"
            + (f"; {len(forms)} other form{'' if len(forms) == 1 else 's'} seen" if forms else ""),
            detail,
        )

    form = contact_forms[0]
    detail["fields"] = form["fields"]
    action = form["action"]
    if not action:
        # A form with no action posts to the page it is on, which just loaded,
        # or it is wired up in JavaScript. Either way there is nothing to probe.
        detail["endpoint"] = None
        return Result(
            "ok",
            "Contact form present (the page submits it itself)",
            detail,
        )

    endpoint = urljoin(page_url, action)
    detail["endpoint"] = endpoint
    status, error = await probe_endpoint(clients.http, endpoint)
    detail["endpoint_status"] = status
    detail["endpoint_error"] = error

    site_host = urlparse(page_url).netloc
    target = short_url(endpoint, site_host)
    if error is not None:
        return Result("fail", f"The form posts to {target}, which does not answer", detail)
    if status in ENDPOINT_MISSING or (status is not None and status >= 500):
        return Result("fail", f"The form posts to {target}, which returns {status}", detail)

    return Result("ok", f"Contact form present, posting to {target} (answers {status})", detail)
