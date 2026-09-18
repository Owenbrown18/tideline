"""Check 4: the domain registration (RDAP).

RDAP is the JSON successor to WHOIS. rdap.org redirects each query to the
registry that owns the TLD (CIRA for .ca, Verisign for .com), whose answer has
an `events` list containing the expiration date and a `status` list of EPP
statuses such as "client hold".

The parsing here is ported from OBdesign's leadgen pipeline
(`Systems/leadgen/domain_status.py`: `_parse_dt`, `_rdap_registrar`,
`check_rdap` and `LAPSED_EPP_STATUSES`), where it has classified hundreds of
real domains. What changed in the port: `httpx` async instead of `requests`,
no on-disk cache (the check runs twice a month, so there is nothing to save), and
the verdict is a Tideline Result instead of a lead tier.

Transient trouble (rdap.org rate limiting, timeouts, 5xx) returns None: a
lookup that could not happen says nothing about the domain.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from tideline.brand import human_date
from tideline.checks.base import Clients, Config, Result

RDAP_TIMEOUT_SECONDS = 12.0
WARN_DAYS = 30
CRITICAL_DAYS = 7

# EPP statuses that mean the registration has lapsed or been suspended.
LAPSED_EPP_STATUSES = {
    "redemptionperiod": "in redemption (expired, recoverable for a short window)",
    "redemption period": "in redemption (expired, recoverable for a short window)",
    "pendingdelete": "pending deletion (about to drop off the registry)",
    "pending delete": "pending deletion (about to drop off the registry)",
    "clienthold": "on registrar hold: the registry has stopped resolving it",
    "client hold": "on registrar hold: the registry has stopped resolving it",
    "serverhold": "on registry hold: it has stopped resolving",
    "server hold": "on registry hold: it has stopped resolving",
}


def parse_dt(value: object) -> datetime | None:
    """Parse an RDAP timestamp into an aware datetime, or None."""
    if not value:
        return None
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def registrar_name(payload: dict[str, Any]) -> str:
    """Registrar name out of the RDAP entity graph (vcardArray 'fn' field)."""
    for entity in payload.get("entities") or []:
        roles = [str(r).lower() for r in (entity.get("roles") or [])]
        if "registrar" not in roles:
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) > 1:
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    return str(item[3])
        if entity.get("handle"):
            return str(entity["handle"])
    return ""


@dataclass(frozen=True)
class RdapInfo:
    registrar: str
    expiry: datetime | None
    statuses: list[str] = field(default_factory=list)
    lapsed_reason: str = ""


def parse_rdap(payload: dict[str, Any]) -> RdapInfo:
    expiry = None
    for event in payload.get("events") or []:
        if str(event.get("eventAction", "")).lower() in ("expiration", "expiry"):
            expiry = parse_dt(event.get("eventDate"))
            break
    statuses = [str(s) for s in (payload.get("status") or [])]
    lapsed_reason = ""
    for status in statuses:
        key = status.lower().replace("_", " ").strip()
        if key in LAPSED_EPP_STATUSES:
            lapsed_reason = LAPSED_EPP_STATUSES[key]
            break
    return RdapInfo(registrar_name(payload), expiry, statuses, lapsed_reason)


def evaluate(
    domain: str,
    info: RdapInfo,
    now: datetime,
    warn_days: int = WARN_DAYS,
    critical_days: int = CRITICAL_DAYS,
) -> Result:
    detail: dict[str, Any] = {
        "domain": domain,
        "registrar": info.registrar,
        "expiry": info.expiry.date().isoformat() if info.expiry else None,
        "days_left": (info.expiry - now).days if info.expiry else None,
        "statuses": info.statuses,
    }
    if info.lapsed_reason:
        return Result("fail", f"The domain is {info.lapsed_reason}", detail)
    if info.expiry is None:
        return Result("ok", "Registered (the registry publishes no expiry date)", detail)

    days_left = (info.expiry - now).days
    expiry = human_date(info.expiry)
    if days_left < 0:
        return Result("fail", f"Registration expired on {expiry}", detail)
    if days_left < critical_days:
        return Result("fail", f"Registration expires in {days_left} days, on {expiry}", detail)
    if days_left < warn_days:
        return Result("warn", f"Registration expires in {days_left} days, on {expiry}", detail)
    return Result("ok", f"Registered until {expiry} ({days_left} days)", detail)


async def run(config: Config, clients: Clients) -> Result | None:
    domain: str = config["domain"]
    try:
        response = await clients.http.get(
            clients.rdap_base_url + domain,
            headers={"Accept": "application/rdap+json"},
            follow_redirects=True,
            timeout=RDAP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return None
    if response.status_code == 404:
        # The registry has no such registration. For a site we know is live this
        # is alarming, but RDAP gaps exist, so it is a warning, not critical.
        return Result(
            "warn",
            "No registration record found (RDAP 404)",
            {"domain": domain, "rdap_status": 404},
        )
    if response.status_code >= 400:
        return None  # 429 rate limit or a registry outage: try again next run
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return evaluate(
        domain,
        parse_rdap(payload),
        clients.now(),
        int(config.get("warn_days", WARN_DAYS)),
        int(config.get("critical_days", CRITICAL_DAYS)),
    )
