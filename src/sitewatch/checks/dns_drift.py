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

from sitewatch.checks.base import Clients, Config, Result
from sitewatch.checks.resolver import DnsUnavailable, DomainMissing, zone_records


def diff_records(
    baseline: dict[str, dict[str, list[str]]], observed: dict[str, dict[str, list[str]]]
) -> list[str]:
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
    baseline: dict[str, dict[str, list[str]]] | None = config.get("baseline")

    try:
        observed = await zone_records(clients.resolver, names)
    except DomainMissing as exc:
        return Result(
            "fail",
            f"DNS for {domain} is broken: {exc}",
            {"domain": domain, "error": str(exc)},
        )
    except DnsUnavailable:
        return None  # our lookup failed; that is not the domain's fault

    record_count = sum(len(values) for types in observed.values() for values in types.values())
    detail: dict[str, Any] = {"domain": domain, "records": observed, "record_count": record_count}

    if baseline is None:
        detail["baseline_captured"] = True
        return Result("ok", f"DNS baseline captured for {domain} ({record_count} records)", detail)

    changes = diff_records(baseline, observed)
    if not changes:
        return Result("ok", f"DNS matches the baseline ({record_count} records)", detail)

    detail["baseline"] = baseline
    detail["changes"] = changes
    summary = f"DNS for {domain} changed: " + "; ".join(changes[:4])
    if len(changes) > 4:
        summary += f" (and {len(changes) - 4} more)"
    return Result("fail", summary, detail)
