"""Authentication.

Two doors, both closed by default:
- the JSON API takes `Authorization: Bearer <TIDELINE_API_TOKEN>`
- the HTML dashboard takes a signed session cookie, set by the sign-in page
  (TIDELINE_DASHBOARD_USER/PASSWORD). HTTP basic auth with the same login also
  works, for scripts and tests.

Why a sign-in page and not the browser's own basic-auth box: behind a Lambda
function URL the box never appears. Lambda renames the WWW-Authenticate header
that asks for it, and CloudFront will not run a function on a 401 to rename it
back (measured 2026-09-18).

The session cookie holds only an expiry time and an HMAC signature of it. The
signing key is derived from the dashboard password, so changing the password
signs every browser out. Only `/healthz`, `/login` and `/static` are open.
Comparisons use `secrets.compare_digest`, which takes the same time whether the
first character is wrong or the last, so nothing can be guessed a character at
a time.
"""

import hashlib
import hmac
import secrets
import time
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from tideline.config import Settings


def same(given: str, expected: str) -> bool:
    """Constant-time comparison that also accepts any characters.

    `secrets.compare_digest` raises on non-ASCII text, which turned a password
    with an accent in it into a server error (found by review, 2026-09-18).
    Comparing the UTF-8 bytes has neither problem.
    """
    return secrets.compare_digest(given.encode(), expected.encode())


bearer_scheme = HTTPBearer(auto_error=False, description="TIDELINE_API_TOKEN")
basic_scheme = HTTPBasic(auto_error=False, realm="Tideline")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
BasicCredentials = Annotated[HTTPBasicCredentials | None, Depends(basic_scheme)]


async def require_api_token(request: Request, credentials: BearerCredentials) -> None:
    expected = _settings(request).api_token
    if not credentials or not same(credentials.credentials, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="a bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


SESSION_COOKIE = "tideline_session"
SESSION_SECONDS = 30 * 24 * 3600


def _session_key(settings: Settings) -> bytes:
    return hashlib.sha256(b"tideline-session:" + settings.dashboard_password.encode()).digest()


def _sign(settings: Settings, payload: str) -> str:
    return hmac.new(_session_key(settings), payload.encode(), hashlib.sha256).hexdigest()


def make_session(settings: Settings, now: float | None = None) -> str:
    """A cookie value: "<expiry>.<signature>"."""
    expires = str(int((now or time.time()) + SESSION_SECONDS))
    return f"{expires}.{_sign(settings, expires)}"


def valid_session(settings: Settings, value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    expires, signature = value.split(".", 1)
    if not same(signature, _sign(settings, expires)):
        return False
    return expires.isdigit() and int(expires) > (now or time.time())


def valid_login(settings: Settings, username: str, password: str) -> bool:
    # Both compared every time, so a wrong username takes as long as a wrong password.
    user_ok = same(username, settings.dashboard_user)
    password_ok = same(password, settings.dashboard_password)
    return user_ok and password_ok


def safe_next(path: str | None) -> str:
    """Where to go after signing in: a path on this site only, never elsewhere."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


async def require_dashboard_user(request: Request, credentials: BasicCredentials) -> str:
    settings = _settings(request)
    if valid_session(settings, request.cookies.get(SESSION_COOKIE)):
        return settings.dashboard_user
    if credentials is not None and valid_login(
        settings, credentials.username, credentials.password
    ):
        return credentials.username
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        # A person in a browser: send them to the sign-in page, then back here.
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(
            status.HTTP_303_SEE_OTHER, headers={"Location": f"/login?next={quote(target)}"}
        )
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="sign in to see the dashboard",
        headers={"WWW-Authenticate": 'Basic realm="Tideline"'},
    )
