"""The API and dashboard, against a real database."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport

from tests.integration.conftest import DATABASE_URL
from tideline.api.app import create_app
from tideline.config import Settings
from tideline.db.models import Check, CheckResult, Incident, Site

TOKEN = "test-token"
DASH = ("owen", "test-password")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=DATABASE_URL or "",
        api_token=TOKEN,
        dashboard_user=DASH[0],
        dashboard_password=DASH[1],
    )


@pytest.fixture
async def client(settings: Settings, sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://tideline.test"
        ) as client,
        app.router.lifespan_context(app),
    ):
        yield client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def data(sessionmaker) -> dict[str, int]:
    """One site, an uptime check, results either side of one resolved incident."""
    now = datetime.now(UTC)
    async with sessionmaker() as session, session.begin():
        site = Site(
            name="Daves' Bakery",
            domain="davesbakery.ca",
            urls=["https://davesbakery.ca/"],
            expected_text="Daves' Bakery",
        )
        session.add(site)
        await session.flush()
        uptime = Check(
            site_id=site.id,
            kind="uptime",
            key="uptime:https://davesbakery.ca/",
            interval_seconds=300,
            config={"url": "https://davesbakery.ca/"},
        )
        tls = Check(
            site_id=site.id,
            kind="tls",
            key="tls:davesbakery.ca",
            interval_seconds=21600,
            config={"hostname": "davesbakery.ca"},
        )
        session.add_all([uptime, tls])
        await session.flush()
        for minutes, status, total_ms in (
            (30, "ok", 100),
            (25, "fail", 0),
            (20, "fail", 0),
            (15, "ok", 300),
            (5, "ok", 200),
        ):
            session.add(
                CheckResult(
                    check_id=uptime.id,
                    started_at=now - timedelta(minutes=minutes),
                    duration_ms=total_ms,
                    status=status,
                    detail={"summary": f"HTTP in {total_ms} ms", "total_ms": total_ms},
                )
            )
        session.add(
            CheckResult(
                check_id=tls.id,
                started_at=now - timedelta(minutes=10),
                duration_ms=120,
                status="warn",
                detail={"summary": "certificate expires in 12 days (2026-09-29)"},
            )
        )
        session.add(
            Incident(
                check_id=uptime.id,
                opened_at=now - timedelta(minutes=25),
                resolved_at=now - timedelta(minutes=15),
                severity="critical",
                summary="https://davesbakery.ca/ is down: HTTP 503",
            )
        )
        session.add(
            Incident(
                check_id=tls.id,
                opened_at=now - timedelta(minutes=10),
                severity="warning",
                summary="certificate expires in 12 days (2026-09-29)",
            )
        )
        await session.flush()
        return {"site": site.id, "uptime": uptime.id, "tls": tls.id}


# --- health and auth ---------------------------------------------------------


async def test_healthz_needs_no_token(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


@pytest.mark.parametrize("path", ["/sites", "/sites/1", "/sites/1/uptime", "/incidents"])
async def test_api_requires_a_token(client, path):
    assert (await client.get(path)).status_code == 401


async def test_wrong_token_is_rejected(client):
    response = await client.get("/sites", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_dashboard_requires_basic_auth(client):
    anonymous = await client.get("/")
    assert anonymous.status_code == 401
    assert "Basic" in anonymous.headers["www-authenticate"]
    assert (await client.get("/", auth=("owen", "wrong"))).status_code == 401


def test_api_refuses_to_start_without_credentials():
    with pytest.raises(RuntimeError, match="TIDELINE_API_TOKEN"):
        create_app(Settings(api_token="", dashboard_password="x"))
    with pytest.raises(RuntimeError, match="TIDELINE_DASHBOARD_PASSWORD"):
        create_app(Settings(api_token="x", dashboard_password=""))


# --- JSON endpoints -----------------------------------------------------------


async def test_list_sites_shows_the_worst_check_status(client, data):
    response = await client.get("/sites", headers=auth())
    assert response.status_code == 200
    [site] = response.json()
    assert site["domain"] == "davesbakery.ca"
    assert site["status"] == "warn"  # uptime ok, TLS warning
    assert site["open_incidents"] == 1


async def test_site_detail_lists_checks_and_open_incidents(client, data):
    site = (await client.get(f"/sites/{data['site']}", headers=auth())).json()
    assert {c["kind"] for c in site["checks"]} == {"uptime", "tls"}
    uptime = next(c for c in site["checks"] if c["kind"] == "uptime")
    assert uptime["status"] == "ok"  # the newest result, not the older failures
    assert uptime["summary"] == "HTTP in 200 ms"
    [incident] = site["open_incident_list"]
    assert incident["check_kind"] == "tls"
    assert incident["resolved_at"] is None


async def test_uptime_window(client, data):
    stats = (await client.get(f"/sites/{data['site']}/uptime?days=30", headers=auth())).json()
    assert stats["results"] == 5
    assert stats["ok"] == 3
    assert stats["uptime_percent"] == 60.0
    assert stats["p50_ms"] == 100
    assert stats["p95_ms"] == 280
    assert stats["incidents"] == 1
    assert 595 <= stats["downtime_seconds"] <= 605  # the 10-minute outage


async def test_uptime_rejects_silly_windows(client, data):
    assert (
        await client.get(f"/sites/{data['site']}/uptime?days=0", headers=auth())
    ).status_code == 422
    assert (
        await client.get(f"/sites/{data['site']}/uptime?days=999", headers=auth())
    ).status_code == 422


async def test_incidents_filtering(client, data):
    everything = (await client.get("/incidents", headers=auth())).json()
    assert len(everything) == 2
    assert everything[0]["opened_at"] > everything[1]["opened_at"]  # newest first

    open_only = (await client.get("/incidents?open=true", headers=auth())).json()
    assert [i["check_kind"] for i in open_only] == ["tls"]
    assert open_only[0]["duration_seconds"] > 0

    resolved = next(i for i in everything if i["resolved_at"])
    assert 595 <= resolved["duration_seconds"] <= 605


async def test_unknown_site_is_404(client):
    assert (await client.get("/sites/9999", headers=auth())).status_code == 404
    assert (await client.get("/sites/9999/uptime", headers=auth())).status_code == 404


async def test_openapi_documents_the_endpoints(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/sites/{site_id}/uptime" in paths
    assert "/" not in paths  # the dashboard stays out of the API docs


# --- dashboard ----------------------------------------------------------------


async def test_dashboard_lists_sites_and_incidents(client, data):
    page = await client.get("/", auth=DASH)
    assert page.status_code == 200
    body = page.text
    # Jinja escapes the apostrophe, which is exactly what should happen to any
    # site name that reaches the page.
    assert "Daves&#39; Bakery" in body
    assert "davesbakery.ca" in body
    # The outage is resolved; the certificate warning is still open.
    assert "The site is up" in body
    assert "One warning to look at when you have time." in body
    assert "1 warning" in body  # the "Needs you" row
    assert "Certificate" in body  # which check the warning is on
    assert "never to clients" in body


async def test_dashboard_site_page(client, data):
    page = await client.get(f"/sites/{data['site']}/view", auth=DASH)
    assert page.status_code == 200
    assert "davesbakery.ca" in page.text
    assert "HTTP in 200 ms" in page.text  # recent results table
    assert "10 min" in page.text  # the resolved incident's duration


async def test_dashboard_empty_state(client):
    page = await client.get("/", auth=DASH)
    assert page.status_code == 200
    assert "Waiting for the first checks" in page.text
    assert "No sites yet" in page.text


async def test_incidents_page_lists_open_and_resolved(client, data):
    page = await client.get("/incidents/view", auth=DASH)
    assert page.status_code == 200
    assert "certificate expires in 12 days" in page.text  # open warning
    assert "is down: HTTP 503" in page.text  # resolved outage
    # The JSON API keeps its own /incidents, behind the token.
    assert (await client.get("/incidents", auth=DASH)).status_code == 401


async def test_reports_page_before_any_rollups(client, data):
    page = await client.get("/reports", auth=DASH)
    assert page.status_code == 200
    assert "No reports yet" in page.text


@pytest.mark.parametrize(
    ("status", "shape"),
    [("fail", "<rect x="), ("warn", 'd="M50 38'), ("ok", '<circle cx="49.5" cy="45.5" r="6"')],
)
async def test_favicon_shows_the_worst_status(client, sessionmaker, status, shape):
    async with sessionmaker() as session, session.begin():
        site = Site(name="One", domain="one.ca", urls=["https://one.ca/"])
        session.add(site)
        await session.flush()
        check = Check(site_id=site.id, kind="uptime", key="u", interval_seconds=300, config={})
        session.add(check)
        await session.flush()
        session.add(
            CheckResult(
                check_id=check.id,
                started_at=datetime.now(UTC),
                duration_ms=1,
                status=status,
                detail={},
            )
        )
    icon = await client.get("/favicon.svg")  # browsers ask for it without credentials
    assert icon.status_code in (200, 401)
    icon = await client.get("/favicon.svg", auth=DASH)
    assert icon.headers["content-type"] == "image/svg+xml"
    assert icon.headers["cache-control"] == "no-cache"
    assert shape in icon.text


async def test_favicon_before_any_checks_is_a_hollow_ring(client):
    icon = await client.get("/favicon.svg", auth=DASH)
    assert 'fill="none"' in icon.text
