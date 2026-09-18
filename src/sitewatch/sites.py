"""sites.yaml: the list of sites to watch, and seeding it into the database.

The real file is git-ignored so the repo can be public without publishing
client configuration. `sites.example.yaml` documents the format.

Seeding is idempotent and safe to run on every deploy:
- sites are matched by domain, checks by (site, key), and updated in place;
- a check no longer produced by the file is disabled, not deleted;
- a site removed from the file is marked inactive, not deleted.
History (results, incidents) is never thrown away by a seed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sitewatch.checks import DEFAULT_INTERVALS
from sitewatch.db.models import Check, Incident, Site

SEEDED_KINDS = ("uptime", "content", "tls", "domain", "dns", "email_auth", "links", "form")


class SiteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    domain: str
    expected_text: str | None = None
    urls: list[str] = Field(default_factory=list)
    intervals: dict[str, int] = Field(default_factory=dict)
    checks: dict[str, bool] = Field(default_factory=dict)
    spam_ignore: list[str] = Field(default_factory=list)
    # The contact page. Defaults to https://<domain>/contact; set "" to switch
    # the form check off for a site that has no contact page.
    contact_url: str | None = None
    # False keeps the site and its history but stops checking it.
    active: bool = True

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, value: str) -> str:
        value = value.strip().lower()
        if "/" in value or " " in value:
            raise ValueError("domain must be a bare hostname like example.ca")
        return value

    @field_validator("intervals", "checks")
    @classmethod
    def _known_kinds(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - set(SEEDED_KINDS)
        if unknown:
            raise ValueError(f"unknown check kinds: {sorted(unknown)}")
        return value

    def page_urls(self) -> list[str]:
        return self.urls or [f"https://{self.domain}/"]


class SitesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: dict[str, dict[str, int]] = Field(default_factory=dict)
    sites: list[SiteEntry]


def load_sites_file(path: Path) -> SitesFile:
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found. Copy sites.example.yaml to sites.yaml and fill it in."
        )
    return SitesFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class CheckSpec:
    kind: str
    key: str
    interval_seconds: int
    config: dict[str, Any]
    enabled: bool


def check_specs(entry: SiteEntry, defaults: dict[str, dict[str, int]]) -> list[CheckSpec]:
    intervals = DEFAULT_INTERVALS | defaults.get("intervals", {}) | entry.intervals

    def enabled(kind: str) -> bool:
        return entry.checks.get(kind, True)

    specs = []
    for url in entry.page_urls():
        specs.append(
            CheckSpec(
                "uptime", f"uptime:{url}", intervals["uptime"], {"url": url}, enabled("uptime")
            )
        )
        content_config: dict[str, Any] = {"url": url}
        if entry.spam_ignore:
            content_config["spam_ignore"] = entry.spam_ignore
        specs.append(
            CheckSpec(
                "content",
                f"content:{url}",
                intervals["content"],
                content_config,
                enabled("content"),
            )
        )
    specs.append(
        CheckSpec(
            "tls",
            f"tls:{entry.domain}",
            intervals["tls"],
            {"hostname": entry.domain},
            enabled("tls"),
        )
    )
    specs.append(
        CheckSpec("domain", f"domain:{entry.domain}", intervals["domain"], {}, enabled("domain"))
    )
    specs.append(CheckSpec("dns", f"dns:{entry.domain}", intervals["dns"], {}, enabled("dns")))
    specs.append(
        CheckSpec(
            "links",
            f"links:{entry.page_urls()[0]}",
            intervals["links"],
            {"url": entry.page_urls()[0]},
            enabled("links"),
        )
    )
    form_config = {"contact_url": entry.contact_url} if entry.contact_url else {}
    specs.append(
        CheckSpec(
            "form",
            f"form:{entry.domain}",
            intervals["form"],
            form_config,
            # A site with no contact page says so rather than failing daily.
            enabled("form") and entry.contact_url != "",
        )
    )
    specs.append(
        CheckSpec(
            "email_auth",
            f"email_auth:{entry.domain}",
            intervals["email_auth"],
            {},
            enabled("email_auth"),
        )
    )
    return specs


@dataclass
class SeedReport:
    sites_added: int = 0
    sites_updated: int = 0
    sites_deactivated: int = 0
    checks_added: int = 0
    checks_updated: int = 0
    checks_disabled: int = 0
    incidents_closed: int = 0


async def seed(session: AsyncSession, sites_file: SitesFile) -> SeedReport:
    """Make the database match the file. The caller commits."""
    report = SeedReport()
    turned_off: list[Check] = []
    existing = {
        site.domain: site
        for site in await session.scalars(select(Site).options(selectinload(Site.checks)))
    }

    for entry in sites_file.sites:
        site = existing.get(entry.domain)
        if site is None:
            site = Site(domain=entry.domain, checks=[])
            session.add(site)
            report.sites_added += 1
        else:
            report.sites_updated += 1
        site.name = entry.name
        site.urls = entry.page_urls()
        site.expected_text = entry.expected_text
        site.active = entry.active

        by_key = {check.key: check for check in site.checks}
        wanted = check_specs(entry, sites_file.defaults)
        for spec in wanted:
            check = by_key.get(spec.key)
            if check is None:
                site.checks.append(
                    Check(
                        kind=spec.kind,
                        key=spec.key,
                        interval_seconds=spec.interval_seconds,
                        config=spec.config,
                        enabled=spec.enabled,
                    )
                )
                report.checks_added += 1
                continue
            was_enabled = check.enabled
            before = (check.interval_seconds, check.config, check.enabled)
            check.kind = spec.kind
            check.interval_seconds = spec.interval_seconds
            check.config = spec.config
            check.enabled = spec.enabled
            if before != (spec.interval_seconds, spec.config, spec.enabled):
                report.checks_updated += 1
            if was_enabled and not spec.enabled:
                turned_off.append(check)

        wanted_keys = {spec.key for spec in wanted}
        for check in site.checks:
            if check.key not in wanted_keys and check.enabled and check.kind in SEEDED_KINDS:
                check.enabled = False
                report.checks_disabled += 1
                turned_off.append(check)

    listed = {entry.domain for entry in sites_file.sites}
    for domain, site in existing.items():
        if domain not in listed and site.active:
            site.active = False
            report.sites_deactivated += 1

    await session.flush()

    # A check that is switched off can never produce the ok result that would
    # close its incident, so switching it off closes it. Without this, disabling
    # the form check on a site with no contact form left its incident open for
    # ever (found on 2026-09-17).
    if turned_off:
        closed = await session.execute(
            update(Incident)
            .where(
                Incident.check_id.in_([check.id for check in turned_off]),
                Incident.resolved_at.is_(None),
            )
            .values(resolved_at=datetime.now(UTC), summary=Incident.summary + " (check turned off)")
        )
        report.incidents_closed = getattr(closed, "rowcount", 0) or 0

    await session.flush()
    return report
