"""The FastAPI application: JSON endpoints plus a server-rendered dashboard.

Run it locally with `tideline api`. In production it runs on AWS Lambda
(tideline.aws_lambda.web_handler) behind CloudFront, which serves
status.obwebdesign.ca over HTTPS.
"""

import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from tideline import __version__, brand
from tideline.api import queries, views
from tideline.api.auth import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    make_session,
    require_api_token,
    require_dashboard_user,
    safe_next,
    valid_login,
)
from tideline.api.schemas import (
    CheckOut,
    DnsBaselineOut,
    Health,
    IncidentOut,
    SiteDetailOut,
    SiteOut,
    UptimeOut,
)
from tideline.config import Settings, get_settings
from tideline.db.baselines import set_baseline
from tideline.db.models import CheckResult
from tideline.db.session import make_engine, make_sessionmaker
from tideline.observability.logging import configure_logging
from tideline.reports.monthly import build_report, render_html
from tideline.schedule import next_report

log = logging.getLogger("tideline.api")
HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
TEMPLATES.env.filters["checkname"] = brand.check_name
TEMPLATES.env.filters["numberword"] = brand.number_word
TEMPLATES.env.filters["percent"] = brand.percent
# A fingerprint of the stylesheet and script, added to their URLs, so a deploy
# that changes them is never hidden behind a browser's cached copy.
ASSETS = hashlib.sha256(
    b"".join((HERE / "static" / name).read_bytes() for name in ("tideline.css", "tideline.js"))
).hexdigest()[:10]
TEMPLATES.env.globals["assets"] = ASSETS

MONTH = re.compile(r"(\d{4})-(\d{2})")

# Sent with every response (security review, 2026-09-18). The page's own styles
# use inline style attributes, so styles allow 'unsafe-inline'; scripts do not.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; "
        "frame-ancestors 'self'; form-action 'self'; base-uri 'none'; object-src 'none'"
    ),
    "Strict-Transport-Security": "max-age=31536000",
    "X-Frame-Options": "SAMEORIGIN",  # the report dialog frames this site's own pages
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith("/static/"):
        # Signed-in pages must not stay in the browser's cache after signing out.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def read_only(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """The demo refuses anything that is not a read."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        return JSONResponse({"detail": "the demo is read-only"}, status_code=405)
    return await call_next(request)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


ERROR_WORDS = {
    404: ("Nothing here", "That page does not exist, or the site it was about has been removed."),
    422: ("That address is not quite right", "Check the link: a month looks like 2026-09."),
    409: ("Try that again", "A check run finished at the same moment. Reload and press it again."),
    403: ("Not allowed from there", "That button only works from Tideline's own pages."),
}


async def error_page(request: Request, exc: StarletteHTTPException) -> Response:
    """Errors as a page for a person, as JSON for a script."""
    if not _wants_html(request) or exc.status_code in (303, 401):
        return await http_exception_handler(request, exc)
    title, text = ERROR_WORDS.get(exc.status_code, ("Something went wrong", str(exc.detail)))
    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {"title": title, "text": text, "status": exc.status_code},
        status_code=exc.status_code,
    )


async def server_error_page(request: Request, exc: Exception) -> Response:
    log.exception("unhandled_error", extra={"path": request.url.path})
    if not _wants_html(request):
        return JSONResponse({"detail": "server error"}, status_code=500)
    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {
            "title": "Something went wrong",
            "text": "Tideline hit an error showing this page. It has been logged.",
            "status": 500,
        },
        status_code=500,
    )


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
    last = await session.scalar(select(func.max(CheckResult.started_at)))
    return TEMPLATES.TemplateResponse(
        request,
        template,
        {
            "state": state,
            "last_checked": last,
            "now": datetime.now(UTC),
            "version": __version__,
            "active": active,
            "demo": request.app.state.settings.demo_mode,
            **context,
        },
    )


@dashboard.get("/", response_class=HTMLResponse, summary="Dashboard")
async def dashboard_home(request: Request, session: Session) -> HTMLResponse:
    zone = request.app.state.settings.display_timezone
    return await _page(
        request, session, "index.html", "overview", o=await views.overview(session, zone)
    )


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
    zone = request.app.state.settings.display_timezone
    return await _page(
        request,
        session,
        "reports.html",
        "reports",
        months=months,
        next_report=next_report(datetime.now(UTC), zone),
    )


@dashboard.post("/sites/{site_id}/dns-baseline/accept-form", summary="Accept DNS (dashboard)")
async def dashboard_accept_dns(
    request: Request, site_id: int, session: Session
) -> RedirectResponse:
    """The dashboard's "Accept the new DNS records" button.

    Browsers send basic-auth credentials automatically, so a form on another
    site could otherwise make Owen's browser press this button (cross-site
    request forgery). Refuse any request whose Origin is not this site: either
    the host the request arrived at, or the public address (behind CloudFront
    the app sees the Lambda's own hostname, not status.obwebdesign.ca).
    """
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    ours = {request.url.netloc}
    public_url: str = request.app.state.settings.public_url
    if public_url:
        ours.add(urlparse(public_url).netloc)
    if urlparse(origin).netloc not in ours:
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
async def dashboard_report(
    request: Request, site_id: int, month: str, session: Session, download: bool = False
) -> Any:
    """The same HTML that gets emailed, at a URL, e.g. /reports/1/2026-09.

    `?download=1` sends it as a file, named for the client and the month, to
    attach to an email or keep.
    """
    match = MONTH.fullmatch(month)
    year, month_number = (int(match[1]), int(match[2])) if match else (0, 0)
    if not (2000 <= year <= 2999 and 1 <= month_number <= 12):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="month must look like 2026-09"
        ) from None
    try:
        report = await build_report(session, site_id, year, month_number)
    except LookupError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no site with id {site_id}"
        ) from None
    headers = {}
    if download:
        name = re.sub(r"[^a-z0-9]+", "-", report.site_name.lower()).strip("-")
        headers["Content-Disposition"] = f'attachment; filename="{name}-{month}-report.html"'
    return HTMLResponse(render_html(report), headers=headers)


# --- signing in ----------------------------------------------------------------------

signin = APIRouter(include_in_schema=False)


@signin.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"next": safe_next(next), "error": False, "username": "", "version": __version__},
    )


@signin.post("/login", response_model=None)
async def login(request: Request) -> Response:
    # Parsed by hand: a form this small does not need another dependency.
    body = (await request.body()).decode(errors="replace")
    fields = {k: v[0] for k, v in parse_qs(body).items()}
    settings: Settings = request.app.state.settings
    target = safe_next(fields.get("next"))
    if not valid_login(settings, fields.get("username", ""), fields.get("password", "")):
        # No deliberate delay: the password is 40 random characters, so guessing
        # is hopeless anyway, and on Lambda a delay only adds to the bill.
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "next": target,
                "error": True,
                "username": fields.get("username", ""),
                "version": __version__,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(settings),
        max_age=SESSION_SECONDS,
        httponly=True,  # page scripts cannot read it
        # Secure whenever the dashboard lives on HTTPS (production always does);
        # only a laptop running `tideline api` over plain HTTP goes without.
        secure=request.url.scheme == "https" or settings.public_url.startswith("https://"),
        samesite="lax",  # not sent on a form another site submits
    )
    return response


@signin.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    missing = [name for name in ("api_token", "dashboard_password") if not getattr(settings, name)]
    if missing and not settings.demo_mode:
        # Refuse to start rather than serve client data unprotected.
        raise RuntimeError(
            "missing required settings: " + ", ".join(f"TIDELINE_{m.upper()}" for m in missing)
        )

    app = FastAPI(
        title="Tideline",
        version=__version__,
        summary="Monitoring for the websites OBdesign runs. Tideline by OBdesign.",
        lifespan=lifespan,
        # No public /docs, /redoc or /openapi.json: they listed every route and
        # loaded third-party JavaScript on this domain (security review).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.middleware("http")(security_headers)
    app.add_exception_handler(StarletteHTTPException, error_page)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, server_error_page)
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

    if settings.demo_mode:
        # Read-only and open: the dashboard pages only, no sign-in, no JSON API,
        # and nothing that changes data.
        app.middleware("http")(read_only)
        app.include_router(dashboard)
        return app
    app.include_router(signin)
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
