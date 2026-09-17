"""Response models. These are what FastAPI documents at /docs and validates on the way out."""

from datetime import datetime

from pydantic import BaseModel

from sitewatch.api import queries


class Health(BaseModel):
    status: str
    database: str
    version: str


class CheckOut(BaseModel):
    id: int
    kind: str
    key: str
    enabled: bool
    interval_seconds: int
    status: str | None
    summary: str | None
    last_checked_at: datetime | None
    duration_ms: int | None

    @classmethod
    def of(cls, check: queries.CheckStatus) -> "CheckOut":
        return cls(
            id=check.check_id,
            kind=check.kind,
            key=check.key,
            enabled=check.enabled,
            interval_seconds=check.interval_seconds,
            status=check.status,
            summary=check.summary,
            last_checked_at=check.last_checked_at,
            duration_ms=check.duration_ms,
        )


class SiteOut(BaseModel):
    id: int
    name: str
    domain: str
    active: bool
    status: str | None
    open_incidents: int

    @classmethod
    def of(cls, site: queries.SiteStatus) -> "SiteOut":
        return cls(
            id=site.id,
            name=site.name,
            domain=site.domain,
            active=site.active,
            status=site.status,
            open_incidents=site.open_incidents,
        )


class IncidentOut(BaseModel):
    id: int
    site_id: int
    site_name: str
    domain: str
    check_kind: str
    check_key: str
    severity: str
    summary: str
    opened_at: datetime
    resolved_at: datetime | None
    duration_seconds: int

    @classmethod
    def of(cls, incident: queries.IncidentView) -> "IncidentOut":
        return cls(
            id=incident.id,
            site_id=incident.site_id,
            site_name=incident.site_name,
            domain=incident.domain,
            check_kind=incident.check_kind,
            check_key=incident.check_key,
            severity=incident.severity,
            summary=incident.summary,
            opened_at=incident.opened_at,
            resolved_at=incident.resolved_at,
            duration_seconds=incident.duration_seconds,
        )


class SiteDetailOut(SiteOut):
    checks: list[CheckOut]
    open_incident_list: list[IncidentOut]


class DnsBaselineOut(BaseModel):
    site_id: int
    accepted_at: datetime
    records: dict[str, dict[str, list[str]]]
    incidents_resolved: int


class UptimeOut(BaseModel):
    site_id: int
    days: int
    since: datetime
    results: int
    ok: int
    uptime_percent: float | None
    p50_ms: int | None
    p95_ms: int | None
    incidents: int
    downtime_seconds: int
