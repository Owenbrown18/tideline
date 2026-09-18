"""Check 5: has anything changed in DNS?

Resolves A, AAAA, CNAME, MX, NS and TXT for the apex and `www`, and compares
the answers with the stored baseline. Any difference opens an incident that
stays open until Owen accepts the new records as the baseline
(`POST /sites/{id}/dns-baseline/accept`), because "DNS changed" is not
something that fixes itself: it is either intentional (a migration) or someone
has taken over the domain's mail or website.

The first run has nothing to compare against, so it captures the baseline and
reports ok. The runner writes that baseline; this function stays pure.
"""

from typing import Any

from tideline.checks.base import Clients, Config, Result
from tideline.checks.resolver import DnsUnavailable, DomainMissing, zone_records

Records = dict[str, dict[str, list[str]]]


def ignore_addresses_behind_a_cname(records: Records) -> Records:
    """Drop A and AAAA records for any name that is a CNAME.

    Found on the live sites on 2026-09-17: `www` points at Vercel with a CNAME,
    and Vercel answers with a rotating pair of addresses out of a larger pool
    (216.150.1.129 one hour, 216.150.1.1 the next). Comparing those addresses
    reported "DNS changed" every hour on seven sites, which is exactly the kind
    of noise that teaches someone to ignore alerts.

    The client's own DNS configuration is the CNAME. What the CDN puts behind it
    is the CDN's business and changes without anyone touching the domain, so the
    CNAME is what gets watched.
    """
    pruned: Records = {}
    for name, types in records.items():
        if "CNAME" in types:
            pruned[name] = {k: v for k, v in types.items() if k not in ("A", "AAAA")}
        else:
            pruned[name] = types
    return pruned


def diff_records(baseline: Records, observed: Records) -> list[str]:
    """Human-readable differences, one line per record type that changed."""
    changes = []
    for name in sorted(set(baseline) | set(observed)):
        before, after = baseline.get(name, {}), observed.get(name, {})
        for rdtype in sorted(set(before) | set(after)):
            was, now = before.get(rdtype, []), after.get(rdtype, [])
            if was == now:
                continue
            if not was:
                changes.append(f"{name} {rdtype} added: {', '.join(now)}")
            elif not now:
                changes.append(f"{name} {rdtype} removed (was {', '.join(was)})")
            else:
                changes.append(f"{name} {rdtype}: {', '.join(was)} -> {', '.join(now)}")
    return changes


async def run(config: Config, clients: Clients) -> Result | None:
    domain: str = config["domain"]
    names: list[str] = list(config.get("names") or [domain, f"www.{domain}"])
    baseline: Records | None = config.get("baseline")

    try:
        observed = ignore_addresses_behind_a_cname(await zone_records(clients.resolver, names))
    except DomainMissing as exc:
        return Result(
            "fail",
            f"DNS is broken: {exc}",
            {"domain": domain, "error": str(exc)},
        )
    except DnsUnavailable:
        return None  # our lookup failed; that is not the domain's fault

    record_count = sum(len(values) for types in observed.values() for values in types.values())
    detail: dict[str, Any] = {"domain": domain, "records": observed, "record_count": record_count}

    if baseline is None:
        detail["baseline_captured"] = True
        return Result("ok", f"Baseline captured ({record_count} records)", detail)

    changes = diff_records(baseline, observed)
    if not changes:
        return Result("ok", f"Matches the accepted baseline ({record_count} records)", detail)

    detail["baseline"] = baseline
    detail["changes"] = changes
    summary = "DNS changed: " + "; ".join(changes[:4])
    if len(changes) > 4:
        summary += f" (and {len(changes) - 4} more)"
    return Result("fail", summary, detail)
