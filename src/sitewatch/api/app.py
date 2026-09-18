"""The FastAPI application: JSON endpoints plus a server-rendered dashboard.

Run it with `sitewatch api`, or in production behind Caddy (M3), which
terminates HTTPS for status.obwebdesign.ca.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from sitewatch import __version__, brand
from sitewatch.api import queries, views
from sitewatch.api.auth import require_api_token, require_dashboard_user
from sitewatch.api.schemas import (
    CheckOut,
    DnsBaselineOut,
    Health,
    IncidentOut,
    SiteDetailOut,
    SiteOut,
    UptimeOut,
)
from sitewatch.config import Settings, get_settings
from sitewatch.db.baselines import set_baseline
from sitewatch.db.session import make_engine, make_sessionmaker
from sitewatch.observability.logging import configure_logging
from sitewatch.reports.monthly import build_report, render_html

log = logging.getLogger("sitewatch.api")
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
TEMPLATES.env.filters["checkname"] = brand.check_name
TEMPLATES.env.filters["numberword"] = brand.number_word
TEMPLATES.env.filters["percent"] = brand.percent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.engine = make_engine(settings.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)
    try:
        yield
    finally:
        await app.state.engine.dispose()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]

api = APIRouter(dependencies=[Depends(require_api_token)])
dashboard = APIRouter(dependencies=[Depends(require_dashboard_user)], include_in_schema=False)


@api.get("/sites", summary="Every site with its current status")
async def list_sites(session: Session) -> list[SiteOut]:
    return [SiteOut.of(s) for s in await queries.site_statuses(session)]


@api.get("/sites/{site_id}", summary="One site: checks, latest results, open incidents")
async def get_site(site_id: int, session: Session) -> SiteDetailOut:
    statuses = await queries.site_statuses(session, site_id)
    if not statuses:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}")
    site = statuses[0]
    open_incidents = await queries.incidents(session, open_only=True, site_id=site_id)
    return SiteDetailOut(
        **SiteOut.of(site).model_dump(),
        checks=[CheckOut.of(c) for c in site.checks],
        open_incident_list=[IncidentOut.of(i) for i in open_incidents],
    )


@api.get("/sites/{site_id}/uptime", summary="Uptime % and response-time percentiles")
async def get_uptime(
    site_id: int,
    session: Session,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> UptimeOut:
    statuses = await queries.site_statuses(session, site_id)
    if not statuses:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}")
    stats = await queries.uptime_stats(session, site_id, days)
    return UptimeOut(**vars(stats))


@api.get("/incidents", summary="Incidents, newest first")
async def list_incidents(
    session: Session,
    open: bool = False,
    site_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[IncidentOut]:
    found = await queries.incidents(session, open_only=open, site_id=site_id, limit=limit)
    return [IncidentOut.of(i) for i in found]


async def _accept_dns(session: AsyncSession, site_id: int) -> DnsBaselineOut:
    """Owen's decision, after a DNS change turns out to be intentional.

    The records from the latest DNS check become the baseline, and the open DNS
    incident is resolved. DNS incidents never resolve on their own (see
    incidents/engine.py), because a changed record is either a migration
    someone did or a domain someone took over.
    """
    statuses = await queries.site_statuses(session, site_id)
    if not statuses:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}")

    latest = await queries.latest_dns_records(session, site_id)
    if latest is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="no DNS check has run for this site yet, so there is nothing to accept",
        )
    # The read above already opened this session's transaction; commit it here.
    now = datetime.now(UTC)
    await set_baseline(session, site_id, latest, now)
    resolved = await queries.resolve_dns_incidents(session, site_id, now)
    await session.commit()
    return DnsBaselineOut(
        site_id=site_id, accepted_at=now, records=latest, incidents_resolved=resolved
    )


@api.post(
    "/sites/{site_id}/dns-baseline/accept",
    summary="Accept the current DNS records as the new baseline",
)
async def accept_dns_baseline(site_id: int, session: Session) -> DnsBaselineOut:
    return await _accept_dns(session, site_id)


# --- the dashboard ---------------------------------------------------------------


async def _page(
    request: Request, session: AsyncSession, template: str, active: str, **context: Any
) -> HTMLResponse:
    """Render a dashboard page with what every page needs: the overall state
    (the period in the header), when the last check ran, and the time now."""
    state = await views.overall_state(session)
    last = await session.scalar(text("SELECT max(started_at) FROM check_results"))
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "state": state,
            "last_checked": last,
            "now": datetime.now(UTC),
            "version": __version__,
            "active": active,
            **context,
        },
    )


@dashboard.get("/", response_class=HTMLResponse, summary="Dashboard")
async def dashboard_home(request: Request, session: Session) -> HTMLResponse:
    return await _page(request, session, "index.html", "overview", o=await views.overview(session))


@dashboard.get("/sites/{site_id}/view", response_class=HTMLResponse, summary="One site")
async def dashboard_site(request: Request, site_id: int, session: Session) -> HTMLResponse:
    page = await views.site_page(session, site_id, request.app.state.settings.display_timezone)
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}")
    return await _page(request, session, "site.html", "overview", p=page)


# /incidents is the JSON API; the page sits beside it, as /sites/{id}/view does.
@dashboard.get("/incidents/view", response_class=HTMLResponse, summary="Incidents")
async def dashboard_incidents(request: Request, session: Session) -> HTMLResponse:
    page = await views.incidents_page(session)
    return await _page(request, session, "incidents.html", "incidents", page=page)


@dashboard.get("/reports", response_class=HTMLResponse, summary="Reports")
async def dashboard_reports(request: Request, session: Session) -> HTMLResponse:
    months = await views.reports_page(session)
    return await _page(request, session, "reports.html", "reports", months=months)


@dashboard.post("/sites/{site_id}/dns-baseline/accept-form", summary="Accept DNS (dashboard)")
async def dashboard_accept_dns(
    request: Request, site_id: int, session: Session
) -> RedirectResponse:
    """The dashboard's "Accept the new DNS records" button.

    Browsers send basic-auth credentials automatically, so a form on another
    site could otherwise make Owen's browser press this button (cross-site
    request forgery). Refuse any request whose Origin is not this site.
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if urlparse(origin).netloc != request.url.netloc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="cross-site request refused")
    await _accept_dns(session, site_id)
    return RedirectResponse(f"/sites/{site_id}/view", status_code=status.HTTP_303_SEE_OTHER)


@dashboard.get("/favicon.svg", summary="Favicon")
async def favicon(session: Session) -> Response:
    """The T. mark, with the period drawn in the worst current status."""
    state = await views.overall_state(session)
    return Response(
        brand.favicon_svg(state),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},
    )


@dashboard.get("/reports/{site_id}/{month}", response_class=HTMLResponse, summary="Monthly report")
async def dashboard_report(request: Request, site_id: int, month: str, session: Session) -> Any:
    """The same HTML that gets emailed, at a URL, e.g. /reports/1/2026-09."""
    try:
        year_text, month_text = month.split("-")
        year, month_number = int(year_text), int(month_text)
        if not 1 <= month_number <= 12:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="month must look like 2026-09"
        ) from None
    try:
        report = await build_report(session, site_id, year, month_number)
    except LookupError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}"
        ) from None
    return HTMLResponse(render_html(report))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    missing = [name for name in ("api_token", "dashboard_password") if not getattr(settings, name)]
    if missing:
        # Refuse to start rather than serve client data unprotected.
        raise RuntimeError(
            "missing required settings: " + ", ".join(f"SITEWATCH_{m.upper()}" for m in missing)
        )

    app = FastAPI(
        title="Tideline",
        version=__version__,
        summary="Monitoring for the websites OBdesign runs. Tideline by OBdesign.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    zone = settings.display_timezone
    TEMPLATES.env.filters["local"] = lambda when, fmt: brand.local(when, fmt, zone)
    # The stylesheet is public: it holds no data, and the login prompt needs no styles.
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/healthz", summary="Liveness: process up, database reachable", tags=["health"])
    async def healthz(session: Session) -> Health:
        try:
            await session.execute(text("SELECT 1"))
        except Exception:
            log.exception("healthz_database_unreachable")
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unreachable"
            ) from None
        return Health(status="ok", database="ok", version=__version__)

    app.include_router(api)
    app.include_router(dashboard)
    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep our JSON logging
        access_log=False,  # one line per check result is enough noise
    )
