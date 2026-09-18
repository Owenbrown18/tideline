import asyncio

from alembic import command
from sqlalchemy import func, select

from tests.integration.conftest import alembic_config
from tideline.db.models import Check, Site
from tideline.sites import SitesFile, seed


async def test_models_and_migrations_agree(engine):
    # Fails if someone changed a model without writing the migration.
    await asyncio.to_thread(command.check, alembic_config())


def sites_file(*entries: dict) -> SitesFile:
    return SitesFile.model_validate({"sites": list(entries)})


BAKERY = {"name": "Daves' Bakery", "domain": "davesbakery.ca", "expected_text": "Daves' Bakery"}
BUILDER = {"name": "Grain Construction Co", "domain": "grainconstruction.ca"}


async def count(session, model, *where):
    return await session.scalar(select(func.count()).select_from(model).where(*where))


async def test_seed_creates_sites_and_checks(sessionmaker):
    async with sessionmaker() as session, session.begin():
        report = await seed(session, sites_file(BAKERY, BUILDER))
    # Eight checks per site: uptime, content, tls, domain, dns, email_auth, links, form.
    assert (report.sites_added, report.checks_added) == (2, 16)

    async with sessionmaker() as session:
        site = await session.scalar(select(Site).where(Site.domain == "davesbakery.ca"))
        assert site.urls == ["https://davesbakery.ca/"]
        assert site.active
        kinds = set(await session.scalars(select(Check.kind).where(Check.site_id == site.id)))
        assert kinds == {"uptime", "content", "tls", "domain", "dns", "email_auth", "links", "form"}


async def test_seed_is_idempotent(sessionmaker):
    for _ in range(2):
        async with sessionmaker() as session, session.begin():
            report = await seed(session, sites_file(BAKERY, BUILDER))
    assert report.sites_added == 0
    assert report.checks_added == 0
    assert report.checks_updated == 0
    async with sessionmaker() as session:
        assert await count(session, Site) == 2
        assert await count(session, Check) == 16


async def test_seed_updates_disables_and_deactivates(sessionmaker):
    async with sessionmaker() as session, session.begin():
        await seed(session, sites_file(BAKERY, BUILDER))

    changed = BAKERY | {"intervals": {"uptime": 60}, "checks": {"domain": False}}
    async with sessionmaker() as session, session.begin():
        report = await seed(session, sites_file(changed))
    assert report.sites_deactivated == 1
    assert report.checks_updated == 2

    async with sessionmaker() as session:
        builder = await session.scalar(select(Site).where(Site.domain == "grainconstruction.ca"))
        assert builder.active is False  # kept, with its history, but no longer checked
        uptime = await session.scalar(
            select(Check).where(Check.key == "uptime:https://davesbakery.ca/")
        )
        assert uptime.interval_seconds == 60
        domain = await session.scalar(select(Check).where(Check.key == "domain:davesbakery.ca"))
        assert domain.enabled is False


async def test_disabling_a_check_closes_its_open_incident(sessionmaker):
    """A check that is off can never produce the ok result that would close it."""
    from datetime import UTC, datetime

    from tideline.db.models import Incident

    async with sessionmaker() as session, session.begin():
        await seed(session, sites_file(BAKERY))
    async with sessionmaker() as session, session.begin():
        check = await session.scalar(select(Check).where(Check.kind == "form"))
        session.add(
            Incident(
                check_id=check.id,
                opened_at=datetime.now(UTC),
                severity="critical",
                summary="no contact form found",
            )
        )

    async with sessionmaker() as session, session.begin():
        report = await seed(session, sites_file(BAKERY | {"checks": {"form": False}}))
    assert report.incidents_closed == 1

    async with sessionmaker() as session:
        incident = await session.scalar(select(Incident))
        assert incident.resolved_at is not None
        assert "check turned off" in incident.summary


async def test_removing_a_url_disables_its_checks(sessionmaker):
    two_pages = BAKERY | {"urls": ["https://davesbakery.ca/", "https://davesbakery.ca/menu"]}
    async with sessionmaker() as session, session.begin():
        await seed(session, sites_file(two_pages))
    async with sessionmaker() as session, session.begin():
        report = await seed(session, sites_file(BAKERY))
    assert report.checks_disabled == 2
    async with sessionmaker() as session:
        assert await count(session, Check, Check.enabled.is_(False), Check.key.like("%/menu")) == 2
