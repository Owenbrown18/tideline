"""Check 8: is the contact form still there, and can it still deliver?

A contact form that silently stops working costs a small business real money,
and nobody notices until someone complains they never heard back. Sitewatch
checks the two things that can be checked without sending anything:

1. the contact page still loads and still contains a form, with the fields a
   contact form needs (somewhere to type a message, somewhere for an address);
2. the endpoint that form posts to still exists and answers.

**It never submits the form.** That would email the client, which is the one
thing Sitewatch must never cause, so the endpoint is probed with a request that
creates nothing: OPTIONS, falling back to a HEAD or GET. Formspree, Web3Forms
and the like answer those without recording a submission. A form posting to the
site's own API route is treated the same way.
"""

from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from sitewatch.checks.base import Clients, Config, Result

FORM_TIMEOUT = 10.0

# Endpoint responses that mean "this exists": anything that is not a 404/410,
# and not a server error. Form backends answer OPTIONS or GET with 200, 204,
# 405 (method not allowed, but the route exists) or 422 (validation), all of
# which prove the endpoint is alive.
ENDPOINT_MISSING = (404, 410)


def find_forms(html: str) -> list[dict[str, Any]]:
    """Every <form> in the page, with its action, method and field names."""
    import re

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
    page_url: str = config.get("contact_url") or urljoin(f"https://{domain}/", "contact")

    page = await clients.pages.fetch(page_url)
    if page.error is not None:
        return None  # unreachable site: the uptime check owns that
    if page.status_code is not None and page.status_code >= 400:
        return Result(
            "fail",
            f"the contact page {page_url} returns HTTP {page.status_code}",
            {"contact_url": page_url, "status_code": page.status_code},
        )
    if page.body is None:
        return None

    forms = find_forms(page.body)
    contact_forms = [form for form in forms if looks_like_a_contact_form(form)]
    detail: dict[str, Any] = {
        "contact_url": page_url,
        "final_url": page.final_url,
        "forms_found": len(forms),
        "contact_forms_found": len(contact_forms),
    }

    if not contact_forms:
        return Result(
            "fail",
            f"no contact form found on {page_url}"
            + (f" ({len(forms)} other form(s) on the page)" if forms else ""),
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
            f"contact form present on {page_url} (submitted by the page itself)",
            detail,
        )

    endpoint = urljoin(page.final_url, action)
    detail["endpoint"] = endpoint
    status, error = await probe_endpoint(clients.http, endpoint)
    detail["endpoint_status"] = status
    detail["endpoint_error"] = error

    host = urlparse(endpoint).netloc
    if error is not None:
        return Result("fail", f"the form endpoint {host} is unreachable: {error}", detail)
    if status in ENDPOINT_MISSING:
        return Result("fail", f"the form endpoint {endpoint} returns HTTP {status}", detail)
    if status is not None and status >= 500:
        return Result("fail", f"the form endpoint {host} returns HTTP {status}", detail)

    return Result(
        "ok",
        f"contact form present and its endpoint ({host}) answers HTTP {status}",
        detail,
    )
