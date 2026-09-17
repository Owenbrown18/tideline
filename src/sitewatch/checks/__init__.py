"""The check registry: check kind (the `checks.kind` column) to the function that runs it."""

from sitewatch.checks import content, dns_drift, domain, email_auth, form, links, tls, uptime
from sitewatch.checks.base import CheckFn, Clients, Result

REGISTRY: dict[str, CheckFn] = {
    "uptime": uptime.run,
    "content": content.run,
    "tls": tls.run,
    "domain": domain.run,
    "dns": dns_drift.run,
    "email_auth": email_auth.run,
    "links": links.run,
    "form": form.run,
}

# README section 2 intervals, in seconds. sites.yaml can override per site.
DEFAULT_INTERVALS: dict[str, int] = {
    "uptime": 5 * 60,
    "content": 5 * 60,
    "tls": 6 * 60 * 60,
    "domain": 24 * 60 * 60,
    "dns": 60 * 60,
    "email_auth": 24 * 60 * 60,
    "links": 24 * 60 * 60,
    "form": 24 * 60 * 60,
}

__all__ = ["DEFAULT_INTERVALS", "REGISTRY", "CheckFn", "Clients", "Result"]
