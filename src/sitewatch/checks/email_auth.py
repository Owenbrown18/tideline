"""Check 6: can this domain still send email that is believed?

Three things go wrong on small-business domains, usually after someone adds a
new mail or marketing tool:

1. **No SPF record.** Receivers have nothing to check the sending server against.
2. **Two SPF records.** The spec says a domain must publish one. Two is a
   permanent error, and receivers treat the domain as unauthenticated, so mail
   silently lands in spam. This is the most common way a working setup breaks:
   a tool adds its own record instead of editing the existing one.
3. **Over 10 DNS lookups in SPF.** `include:`, `a`, `mx`, `ptr`, `exists:` and
   `redirect=` each cost a lookup, recursively. Past ten, evaluation fails with
   "permerror", which again means unauthenticated mail.

DMARC is also checked: without it, receivers have no instruction for what to do
with mail that fails, and nobody finds out someone is spoofing the domain.

Sitewatch never sends mail to a client domain to test this: everything here is
a DNS query.
"""

import re
from typing import Any

from sitewatch.checks.base import Clients, Config, Result
from sitewatch.checks.resolver import DnsUnavailable, DomainMissing, Resolver

# Mechanisms that cost a DNS lookup when SPF is evaluated (RFC 7208 section 4.6.4).
LOOKUP_MECHANISMS = re.compile(r"\b(?:include:|a[:/\s]|a$|mx[:/\s]|mx$|ptr\b|exists:|redirect=)")
MAX_SPF_LOOKUPS = 10


def spf_records(txt: list[str]) -> list[str]:
    return [record for record in txt if record.lower().startswith("v=spf1")]


def count_lookups(record: str) -> int:
    """DNS lookups this single record costs, not following includes."""
    return len(LOOKUP_MECHANISMS.findall(record.lower()))


async def total_spf_lookups(
    resolver: Resolver, record: str, depth: int = 0, seen: set[str] | None = None
) -> int:
    """Lookups for a record plus everything it includes, following the chain.

    Stops at 10 (the point where evaluation fails anyway) and at repeats, so a
    domain that includes itself cannot spin forever.
    """
    seen = seen if seen is not None else set()
    total = count_lookups(record)
    if depth >= 5 or total > MAX_SPF_LOOKUPS:
        return total

    for target in re.findall(r"(?:include:|redirect=)([^\s]+)", record, re.IGNORECASE):
        target = target.lower().rstrip(".")
        if target in seen:
            continue
        seen.add(target)
        try:
            nested = spf_records(await resolver.records(target, "TXT"))
        except (DnsUnavailable, DomainMissing):
            continue  # an include that does not resolve is its own problem
        if nested:
            total += await total_spf_lookups(resolver, nested[0], depth + 1, seen)
        if total > MAX_SPF_LOOKUPS:
            break
    return total


def parse_dmarc(txt: list[str]) -> str | None:
    for record in txt:
        if record.lower().startswith("v=dmarc1"):
            return record
    return None


async def run(config: Config, clients: Clients) -> Result | None:
    domain: str = config["domain"]
    try:
        apex_txt = await clients.resolver.records(domain, "TXT")
    except DomainMissing as exc:
        # The domain itself is gone, which is worse than a missing DMARC record.
        return Result("fail", f"The domain does not resolve: {exc}", {"domain": domain})
    except DnsUnavailable:
        return None

    try:
        dmarc_txt = await clients.resolver.records(f"_dmarc.{domain}", "TXT")
    except DomainMissing:
        # NXDOMAIN on _dmarc.<domain> is the normal answer when a domain has no
        # DMARC record at all: the subdomain simply does not exist. It is a
        # missing record, not a broken domain.
        dmarc_txt = []
    except DnsUnavailable:
        return None

    spf = spf_records(apex_txt)
    dmarc = parse_dmarc(dmarc_txt)
    policy_match = re.search(r"\bp\s*=\s*(none|quarantine|reject)", dmarc or "", re.IGNORECASE)

    detail: dict[str, Any] = {
        "domain": domain,
        "spf_records": spf,
        "dmarc_record": dmarc,
        "dmarc_policy": policy_match.group(1).lower() if policy_match else None,
    }

    # Two levels, because these problems are not equally urgent (Owen's call,
    # 2026-09-17, after eight client domains turned out to be missing records
    # without a single complaint from anyone):
    #
    #   fail  mail is actively failing authentication right now: two SPF records
    #         or too many lookups both make receivers treat every message as
    #         unauthenticated.
    #   warn  a gap that leaves the domain spoofable but breaks nothing today:
    #         no SPF at all, or no DMARC.
    breaking: list[str] = []
    gaps: list[str] = []

    if not spf:
        gaps.append("No SPF record, so anyone can send mail as this domain")
    elif len(spf) > 1:
        breaking.append(f"{len(spf)} SPF records (there must be exactly one)")
    else:
        lookups = await total_spf_lookups(clients.resolver, spf[0])
        detail["spf_lookups"] = lookups
        if lookups > MAX_SPF_LOOKUPS:
            breaking.append(f"SPF needs {lookups} DNS lookups, over the limit of {MAX_SPF_LOOKUPS}")

    if dmarc is None:
        gaps.append("No DMARC record")

    if breaking:
        return Result("fail", ". ".join(breaking + gaps), detail)
    if gaps:
        return Result("warn", ". ".join(gaps), detail)

    policy = detail["dmarc_policy"]
    summary = f"SPF valid ({detail.get('spf_lookups', 0)} lookups), DMARC present"
    if policy in ("quarantine", "reject"):
        summary += f" and set to {policy}"
    elif policy == "none":
        summary += ", policy p=none (monitoring only)"
    return Result("ok", summary, detail)
