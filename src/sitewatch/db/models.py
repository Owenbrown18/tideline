"""Database tables (README section 3, "Data model").

Every column here maps to a migration in alembic/versions. Change a model, then
generate a migration with `alembic revision --autogenerate -m "..."` and read it
before committing.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

CHECK_KINDS = ("uptime", "content", "tls", "domain", "dns", "email_auth", "links", "form")
STATUSES = ("ok", "warn", "fail")
SEVERITIES = ("warning", "critical")
ALERT_KINDS = ("open", "escalated", "reminder", "resolved")


def _in(column: str, values: tuple[str, ...]) -> str:
    return "{} IN ({})".format(column, ", ".join(f"'{v}'" for v in values))


class Base(DeclarativeBase):
    # Predictable constraint names, so later migrations can refer to them.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {dict[str, Any]: JSONB}  # noqa: RUF012 (SQLAlchemy API)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text))
    expected_text: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    checks: Mapped[list["Check"]] = relationship(back_populates="site")


class Check(Base):
    __tablename__ = "checks"
    __table_args__ = (
        # `key` makes seeding idempotent: "uptime:https://example.ca/" is one check.
        UniqueConstraint("site_id", "key"),
        CheckConstraint(_in("kind", CHECK_KINDS), name="kind_valid"),
        CheckConstraint("interval_seconds > 0", name="interval_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(Text)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    site: Mapped[Site] = relationship(back_populates="checks")


class CheckResult(Base):
    __tablename__ = "check_results"
    __table_args__ = (
        Index("ix_check_results_check_id_started_at", "check_id", "started_at"),
        CheckConstraint(_in("status", STATUSES), name="status_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(4))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(_in("severity", SEVERITIES), name="severity_valid"),
        # At most one open incident per check, enforced by the database itself.
        Index(
            "uq_incidents_one_open_per_check",
            "check_id",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id", ondelete="CASCADE"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(10))
    summary: Mapped[str] = mapped_column(Text)
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    check: Mapped[Check] = relationship()


class DnsBaseline(Base):
    __tablename__ = "dns_baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    records: Mapped[dict[str, Any]]
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyRollup(Base):
    """One row per site per day, kept forever.

    Raw `check_results` are purged after 90 days (about 3,500 rows a day at 11
    sites), but the numbers a monthly report needs are small and worth keeping:
    uptime, response-time percentiles, incidents and downtime.
    """

    __tablename__ = "daily_rollups"
    __table_args__ = (UniqueConstraint("site_id", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    results: Mapped[int] = mapped_column(Integer)
    ok_results: Mapped[int] = mapped_column(Integer)
    uptime_checks: Mapped[int] = mapped_column(Integer)
    uptime_ok: Mapped[int] = mapped_column(Integer)
    uptime_percent: Mapped[float | None] = mapped_column(Float)
    p50_ms: Mapped[int | None] = mapped_column(Integer)
    p95_ms: Mapped[int | None] = mapped_column(Integer)
    incidents_opened: Mapped[int] = mapped_column(Integer, default=0)
    downtime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    site: Mapped[Site] = relationship()


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (CheckConstraint(_in("kind", ALERT_KINDS), name="kind_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(20))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(10))
