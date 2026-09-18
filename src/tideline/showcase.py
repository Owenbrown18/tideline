"""Showcase data: six months of Tideline for invented businesses.

The real dashboard shows real clients, their names and their security gaps, so
it cannot go in a portfolio photo. This builds a separate database for eight
made-up businesses (their domains were checked to be unregistered on
2026-09-18), by running the real product against a simulated internet:

- every run is `tideline.worker.run.run`, on each 1st and 15th for six months,
  with a fake clock set to that date;
- web pages, forms, links and the domain registry answer through an
  `httpx.MockTransport`, and DNS through a fake resolver, following a script of
  what happened to each site and when;
- the TLS handshake is the one thing simulated directly (it opens a raw
  socket); its verdict still comes from the real `evaluate_expiry`.

So every headline, incident, alert, strip and report in the result is what
Tideline itself produced. Nothing is written by hand.

    uv run tideline showcase --out showcase.db
    TIDELINE_DATABASE_URL=sqlite+aiosqlite:///showcase.db TIDELINE_API_TOKEN=x \\
      TIDELINE_DASHBOARD_PASSWORD=x uv run tideline api
"""

import asyncio
import os
import random
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tideline.checks import REGISTRY, Clients, Result
from tideline.checks.http import PageFetcher
from tideline.checks.tls import evaluate_expiry
from tideline.config import Settings
from tideline.notify.base import AlertMessage
from tideline.schedule import RUN_DAYS, RUN_TIME
from tideline.worker.run import run

ZONE = "America/Vancouver"
# In the repo, alembic.ini is at the root; in the Lambda image, where the
# Dockerfile says (TIDELINE_ALEMBIC_INI).
ALEMBIC_INI = Path(
    os.environ.get("TIDELINE_ALEMBIC_INI", Path(__file__).resolve().parents[2] / "alembic.ini")
)
RUNS = 12  # six months at two runs a month
FORM_ENDPOINT = "https://formspree.io/f/"


@dataclass
class ShowcaseSite:
    """One invented business, and what happens to it run by run (0 = oldest)."""

    name: str
    domain: str
    speed_ms: int  # typical response time
    registrar: str
    domain_days_left: int  # at the latest run
    down_at: set[int] = field(default_factory=set)
    form_broken_from: int | None = None  # the form's endpoint returns 404 from this run
    broken_link_at: set[int] = field(default_factory=set)  # an external link 404s
    dns_change_at: int | None = None  # the A record moves (then Owen accepts it)
    no_dmarc: bool = False
    no_spf: bool = False


SITES = [
    ShowcaseSite(
        "Harbour & Hearth Bakery", "harbourhearthbakery.ca", 290, "Webnames.ca Inc.", 41,
        down_at={4},
    ),
    ShowcaseSite("Tidewater Physiotherapy", "tidewaterphysio.ca", 240, "Tucows Domains Inc.", 212),
    ShowcaseSite(
        "Cedar & Stone Landscaping", "cedarstonelandscaping.ca", 330, "Namecheap, Inc.", 300,
        broken_link_at={7, 8},
    ),
    ShowcaseSite(
        "Ridgeback Roofing", "ridgebackroofing.ca", 410, "GoDaddy.com, LLC", 158,
        down_at={RUNS - 1},
    ),
    ShowcaseSite(
        "Saltspring Pottery Studio", "saltspringpotterystudio.ca", 360, "Webnames.ca Inc.", 95,
        no_dmarc=True,
    ),
    ShowcaseSite(
        "Northwind Dental", "northwinddental.ca", 270, "Tucows Domains Inc.", 250,
        form_broken_from=RUNS - 1,
    ),
    ShowcaseSite(
        "Lantern Cycle Works", "lanterncycleworks.ca", 220, "Namecheap, Inc.", 180,
        dns_change_at=6,
    ),
    ShowcaseSite("Kestrel Music Studio", "kestrelmusicstudio.ca", 310, "GoDaddy.com, LLC", 330),
]  # fmt: skip


def run_times(now: datetime, count: int = RUNS) -> list[datetime]:
    """The last `count` scheduled runs before `now`, oldest first, in UTC."""
    zone = ZoneInfo(ZONE)
    day = now.astimezone(zone).date()
    times: list[datetime] = []
    while len(times) < count:
        if day.day in RUN_DAYS:
            at = datetime.combine(day, RUN_TIME, tzinfo=zone)
            if at <= now:
                times.append(at.astimezone(UTC))
        day -= timedelta(days=1)
    return list(reversed(times))


def sites_yaml() -> str:
    lines = ["sites:"]
    for site in SITES:
        lines += [
            f'  - name: "{site.name}"',
            f"    domain: {site.domain}",
            f'    expected_text: "{site.name}"',
            f"    contact_url: https://{site.domain}/contact",
        ]
    return "\n".join(lines) + "\n"


class World:
    """What the simulated internet looks like at one run."""

    def __init__(self, index: int, at: datetime, final: datetime, pace: float = 1.0) -> None:
        self.index = index
        self.pace = pace
        self.at = at
        self.final = final
        self.by_domain = {s.domain: s for s in SITES}

    def site_for(self, host: str) -> ShowcaseSite | None:
        return self.by_domain.get(host.removeprefix("www."))

    # --- HTTP -------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        site = self.site_for(request.url.host)
        get_page = request.method == "GET" and not request.url.path.startswith("/api")
        if site is not None and get_page and self.pace:
            # Pages take about as long as the real site would, so response times look real.
            jitter = random.Random(f"{self.index}{request.url}").uniform(0.8, 1.25)
            await asyncio.sleep(site.speed_ms * jitter * self.pace / 1000)
        return self._answer(request)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "rdap.showcase":
            return self._rdap(request.url.path.rsplit("/", 1)[-1])
        if host == "formspree.io":
            return httpx.Response(200, text="ok")
        if host.endswith(".example"):  # other people's sites that client pages link to
            broken = any(
                self.index in s.broken_link_at and host.startswith(s.domain.split(".")[0])
                for s in SITES
            )
            return httpx.Response(404 if broken else 200, text="<p>A partner site</p>")
        site = self.site_for(host)
        if site is None:
            raise httpx.ConnectError("unknown host", request=request)
        if self.index in site.down_at:
            return httpx.Response(503, text="Service Unavailable")
        path = request.url.path
        if path == "/api/contact":
            broken = site.form_broken_from is not None and self.index >= site.form_broken_from
            return httpx.Response(404 if broken else 200, text="")
        if path == "/contact":
            return httpx.Response(200, html=self._contact_page(site))
        if path in ("/", "/about", "/services"):
            return httpx.Response(200, html=self._page(site, path))
        return httpx.Response(404, text="Not found")

    def _page(self, site: ShowcaseSite, path: str) -> str:
        partner = f"https://{site.domain.split('.')[0]}-directory.example/listing"
        return (
            f"<title>{site.name}</title><h1>{site.name}</h1>"
            "<nav><a href='/'>Home</a> <a href='/about'>About</a> "
            "<a href='/services'>Services</a> <a href='/contact'>Contact</a></nav>"
            f"<p>{site.name} has served the island since 2011.</p>"
            f"<p><a href='{partner}'>Find us in the local directory</a></p>"
        )

    def _contact_page(self, site: ShowcaseSite) -> str:
        action = (
            "/api/contact"
            if site.form_broken_from is not None
            else f"{FORM_ENDPOINT}x{site.domain[:6]}"
        )
        return (
            f"<title>Contact · {site.name}</title><h1>Contact {site.name}</h1>"
            f"<form action='{action}' method='post'>"
            "<input name='name'><input name='email' type='email'>"
            "<textarea name='message'></textarea><button>Send</button></form>"
        )

    def _rdap(self, domain: str) -> httpx.Response:
        site = self.by_domain[domain]
        expiry = self.final + timedelta(days=site.domain_days_left)
        return httpx.Response(
            200,
            json={
                "objectClassName": "domain",
                "ldhName": domain,
                "status": ["active"],
                "events": [
                    {"eventAction": "registration", "eventDate": "2019-03-04T17:00:00Z"},
                    {"eventAction": "expiration", "eventDate": expiry.isoformat()},
                ],
                "entities": [
                    {
                        "roles": ["registrar"],
                        "vcardArray": ["vcard", [["fn", {}, "text", site.registrar]]],
                    }
                ],
            },
        )

    # --- DNS ----------------------------------------------------------------------

    async def records(self, name: str, rdtype: str) -> list[str]:
        apex = name.removeprefix("_dmarc.").removeprefix("www.")
        site = self.by_domain.get(apex)
        if site is None:
            return []
        if name.startswith("_dmarc."):
            return [] if site.no_dmarc or rdtype != "TXT" else ["v=DMARC1; p=quarantine"]
        if name.startswith("www."):
            return ["cname.vercel-dns.com"] if rdtype == "CNAME" else []
        moved = site.dns_change_at is not None and self.index >= site.dns_change_at
        return {
            "A": ["76.76.21.98" if moved else "76.76.21.21"],
            "MX": ["10 mx1.improvmx.com", "20 mx2.improvmx.com"],
            "NS": ["ns1.dns-parking.com", "ns2.dns-parking.com"],
            "TXT": [] if site.no_spf else ["v=spf1 include:spf.improvmx.com ~all"],
        }.get(rdtype, [])

    # --- TLS: the one simulated handshake -------------------------------------------

    async def tls(self, config: Any, clients: Clients) -> Result | None:
        domain = config["domain"]
        # Certificates renew every 90 days, 30 days before they expire.
        cycle = (self.at - datetime(2026, 1, 1, tzinfo=UTC)).days + zlib.crc32(domain.encode()) % 60
        not_after = self.at + timedelta(days=90 - cycle % 60)
        status, summary = evaluate_expiry(not_after, clients.now())
        days_left = round((not_after - clients.now()).total_seconds() / 86400, 1)
        detail = {
            "hostname": domain,
            "valid": True,
            "not_after": not_after.isoformat(),
            "days_left": days_left,
            "issuer": "Let's Encrypt",
        }
        return Result(status, summary, detail)


class Inbox:
    """Collects what Tideline would have emailed Owen."""

    channel = "ses"  # so the timeline reads "emailed", as it would in production

    def __init__(self) -> None:
        self.subjects: list[str] = []

    async def send(self, msg: AlertMessage) -> None:  # pragma: no cover - the digest collects
        raise AssertionError("alerts go through the run's digest")

    async def send_report(self, subject: str, text: str, html: str) -> str:
        self.subjects.append(subject)
        return "showcase"


@contextmanager
def _simulated_tls(world: World) -> Iterator[None]:
    original = REGISTRY["tls"]
    REGISTRY["tls"] = world.tls
    try:
        yield
    finally:
        REGISTRY["tls"] = original


def build(out: Path, now: datetime | None = None, pace: float = 1.0) -> list[str]:
    """Build the showcase database at `out`, which must not exist yet.

    Returns the subjects of the emails Tideline would have sent Owen. `pace`
    scales the simulated page load times (0 for tests: instant, same results).
    """
    from alembic import command
    from alembic.config import Config

    from tideline.db.session import sync_url

    if out.exists():
        raise FileExistsError(f"{out} already exists")
    url = f"sqlite+aiosqlite:///{out}"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", sync_url(url))
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")

    sites_file = out.with_suffix(".sites.yaml")
    sites_file.write_text(sites_yaml(), encoding="utf-8")
    try:
        return asyncio.run(_runs(url, sites_file, now or datetime.now(UTC), pace))
    finally:
        sites_file.unlink(missing_ok=True)


async def _runs(url: str, sites_file: Path, now: datetime, pace: float) -> list[str]:
    settings = Settings(database_url=url, rdap_base_url="https://rdap.showcase/domain/")
    inbox = Inbox()
    times = run_times(now)
    for index, at in enumerate(times):
        world = World(index, at, times[-1], pace)
        http = httpx.AsyncClient(transport=httpx.MockTransport(world.handle))
        clients = Clients(
            http=http,
            pages=PageFetcher(http, cache_seconds=0),
            resolver=world,
            now=Clock(at),
            sleep=_no_wait,
            uptime_retry_delay_seconds=0,
            rdap_base_url=settings.rdap_base_url,
        )
        with _simulated_tls(world):
            await run(
                settings,
                inbox,
                sites_file=sites_file if index == 0 else None,
                now=at,
                clients=clients,
            )
        await http.aclose()
        await _accept_dns_changes(url, index, at)
    return inbox.subjects


async def _accept_dns_changes(url: str, index: int, at: datetime) -> None:
    """Owen accepts a planned DNS change a day after the run that caught it."""
    from sqlalchemy import select

    from tideline.api.queries import latest_dns_records, resolve_dns_incidents
    from tideline.db.baselines import set_baseline
    from tideline.db.models import Site
    from tideline.db.session import make_engine, make_sessionmaker

    moved = [s for s in SITES if s.dns_change_at == index]
    if not moved:
        return
    engine = make_engine(url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            for site in moved:
                site_id = await session.scalar(select(Site.id).where(Site.domain == site.domain))
                assert site_id is not None
                accepted = at + timedelta(days=1)
                records = await latest_dns_records(session, site_id)
                assert records is not None
                await set_baseline(session, site_id, records, accepted)
                await resolve_dns_incidents(session, site_id, accepted)
    finally:
        await engine.dispose()


@dataclass
class Clock:
    """The time a simulated run happens at."""

    at: datetime

    def __call__(self) -> datetime:
        return self.at


async def _no_wait(_: float) -> None:
    return None
