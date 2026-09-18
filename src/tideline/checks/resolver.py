"""DNS lookups for checks 5 and 6.

Queries go to public resolvers (Cloudflare, then Google) rather than whatever
the machine's resolver is, for two reasons: the answers are then the same from a
laptop and from the EC2 instance, and an ISP resolver that filters or caches
badly cannot invent a DNS "change" that nobody else sees. The same lesson is
written into OBdesign's leadgen classifier, where a single ISP NXDOMAIN nearly
put "your domain no longer exists" into an email about a live domain.

Everything returns plain sorted lists of strings, so records can be compared
and stored as JSON.
"""

import logging
from typing import Protocol, runtime_checkable

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver

log = logging.getLogger("tideline.dns")

PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
TIMEOUT_SECONDS = 6.0

RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT")


class DnsUnavailable(Exception):
    """The lookup itself failed (timeout, no resolver). Says nothing about the domain."""


class DomainMissing(Exception):
    """The name does not exist (NXDOMAIN), confirmed by a public resolver."""


@runtime_checkable
class Resolver(Protocol):
    async def records(self, name: str, rdtype: str) -> list[str]:
        """Sorted record values, or an empty list when the name has none of that type."""


def _normalise(rdtype: str, value: str) -> str:
    value = value.strip()
    if rdtype in ("CNAME", "NS"):
        return value.rstrip(".").lower()
    if rdtype == "MX":
        # "10 mail.example.com." -> "10 mail.example.com"
        parts = value.split()
        if len(parts) == 2:
            return f"{parts[0]} {parts[1].rstrip('.').lower()}"
    if rdtype == "TXT":
        # dnspython quotes TXT strings and splits long ones; join and unquote.
        return value.replace('" "', "").strip('"')
    return value.lower()


class PublicResolver:
    """The real thing: dnspython against the public resolvers."""

    def __init__(self, nameservers: tuple[str, ...] = PUBLIC_RESOLVERS) -> None:
        self._resolver = dns.asyncresolver.Resolver(configure=False)
        self._resolver.nameservers = list(nameservers)
        self._resolver.timeout = TIMEOUT_SECONDS
        self._resolver.lifetime = TIMEOUT_SECONDS

    async def records(self, name: str, rdtype: str) -> list[str]:
        try:
            answer = await self._resolver.resolve(name, rdtype)
        except dns.resolver.NXDOMAIN as exc:
            raise DomainMissing(f"{name} does not exist (NXDOMAIN)") from exc
        except dns.resolver.NoAnswer:
            return []  # the name exists, it just has no record of this type
        except dns.resolver.NoNameservers as exc:
            # SERVFAIL from every authoritative server: the domain's nameservers
            # are broken, which is a real problem rather than a lookup failure.
            raise DomainMissing(f"{name}: SERVFAIL, the nameservers do not answer") from exc
        except dns.exception.DNSException as exc:
            raise DnsUnavailable(f"{name} {rdtype}: {type(exc).__name__}") from exc
        return sorted({_normalise(rdtype, r.to_text()) for r in answer})


async def zone_records(
    resolver: Resolver, names: list[str], types: tuple[str, ...] = RECORD_TYPES
) -> dict[str, dict[str, list[str]]]:
    """Every record of `types` for each name, as {name: {type: [values]}}.

    Types with no records are left out, so a baseline stays readable.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for name in names:
        found: dict[str, list[str]] = {}
        for rdtype in types:
            values = await resolver.records(name, rdtype)
            if values:
                found[rdtype] = values
        out[name] = found
    return out
