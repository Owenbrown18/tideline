"""Check 3: the TLS certificate.

Opens a TLS connection the way a browser would (full chain validation and
hostname match, via the default SSL context) and reads the certificate's
expiry. An invalid certificate is a failure outright; a valid one is judged on
how many days it has left.

Connection problems (port closed, timeout) return None: the site being
unreachable is the uptime check's incident, not a certificate problem.
"""

import asyncio
import ssl
from datetime import UTC, datetime

from tideline.brand import human_date
from tideline.checks.base import Clients, Config, Result, Status

CONNECT_TIMEOUT_SECONDS = 10.0
WARN_DAYS = 21
CRITICAL_DAYS = 7


def evaluate_expiry(
    not_after: datetime,
    now: datetime,
    warn_days: int = WARN_DAYS,
    critical_days: int = CRITICAL_DAYS,
) -> tuple[Status, str]:
    """(status, summary) for a certificate expiring at `not_after`."""
    days_left = (not_after - now).total_seconds() / 86400
    expiry = human_date(not_after)
    if days_left < 0:
        return "fail", f"Certificate expired on {expiry}"
    if days_left < critical_days:
        return "fail", f"Certificate expires in {int(days_left)} days, on {expiry}"
    if days_left < warn_days:
        return "warn", f"Certificate expires in {int(days_left)} days, on {expiry}"
    return "ok", f"Valid for {int(days_left)} more days, until {expiry}"


async def run(config: Config, clients: Clients) -> Result | None:
    hostname: str = config.get("hostname") or config["domain"]
    port: int = int(config.get("port", 443))
    connect_host: str = config.get("connect_host") or hostname

    writer = None
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
            _, writer = await asyncio.open_connection(
                connect_host, port, ssl=clients.ssl_context, server_hostname=hostname
            )
        cert = writer.get_extra_info("peercert")
    except ssl.SSLCertVerificationError as exc:
        reason = exc.verify_message or str(exc)
        return Result(
            "fail",
            f"Invalid certificate for {hostname}: {reason}",
            {"hostname": hostname, "valid": False, "error": reason},
        )
    except (TimeoutError, OSError):
        # ssl.SSLError is an OSError too: a handshake that fails for reasons other
        # than verification is still a connection problem, not a certificate verdict.
        return None
    finally:
        if writer is not None:
            writer.close()

    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), UTC)
    status, summary = evaluate_expiry(
        not_after,
        clients.now(),
        int(config.get("warn_days", WARN_DAYS)),
        int(config.get("critical_days", CRITICAL_DAYS)),
    )
    issuer = dict(item[0] for item in cert.get("issuer", ()))
    detail = {
        "hostname": hostname,
        "valid": True,
        "not_after": not_after.isoformat(),
        "days_left": round((not_after - clients.now()).total_seconds() / 86400, 1),
        "issuer": issuer.get("organizationName") or issuer.get("commonName"),
    }
    return Result(status, summary, detail)
