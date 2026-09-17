"""Authentication.

Two doors, both closed by default:
- the JSON API takes `Authorization: Bearer <SITEWATCH_API_TOKEN>`
- the HTML dashboard takes HTTP basic auth (SITEWATCH_DASHBOARD_USER/PASSWORD)

Only `/healthz` is open, so the load balancer and CloudWatch can poll it.
Comparisons use `secrets.compare_digest`, which takes the same time whether the
first character is wrong or the last, so a token cannot be guessed one
character at a time.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from sitewatch.config import Settings

bearer_scheme = HTTPBearer(auto_error=False, description="SITEWATCH_API_TOKEN")
basic_scheme = HTTPBasic(auto_error=False, realm="Sitewatch")


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
BasicCredentials = Annotated[HTTPBasicCredentials | None, Depends(basic_scheme)]


async def require_api_token(request: Request, credentials: BearerCredentials) -> None:
    expected = _settings(request).api_token
    if not credentials or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="a bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_dashboard_user(request: Request, credentials: BasicCredentials) -> str:
    settings = _settings(request)
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="sign in to see the dashboard",
        headers={"WWW-Authenticate": 'Basic realm="Sitewatch"'},
    )
    if credentials is None:
        raise unauthorized
    user_ok = secrets.compare_digest(credentials.username, settings.dashboard_user)
    password_ok = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not (user_ok and password_ok):
        raise unauthorized
    return credentials.username
