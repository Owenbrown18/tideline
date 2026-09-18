"""The public demo: the dashboard with no sign-in, read-only, over invented data."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx
import pytest
from httpx import ASGITransport
from moto import mock_aws

from tideline.api.app import create_app
from tideline.config import Settings, get_settings
from tideline.showcase import build

HTML = {"accept": "text/html"}


@pytest.fixture(scope="module")
def showcase_url(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("demo") / "showcase.db"
    build(out, now=datetime(2026, 9, 18, 19, 0, tzinfo=UTC), pace=0)
    return f"sqlite+aiosqlite:///{out}"


@pytest.fixture
async def demo(showcase_url) -> AsyncIterator[httpx.AsyncClient]:
    # No API token, no password: demo mode needs neither.
    app = create_app(Settings(database_url=showcase_url, demo_mode=True))
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://demo.test") as c,
        app.router.lifespan_context(app),
    ):
        yield c


async def test_anyone_can_look_around(demo):
    for path in ("/", "/incidents/view", "/reports", "/sites/1/view"):
        page = await demo.get(path, headers=HTML)
        assert page.status_code == 200, path
        assert "Live demo." in page.text
        assert "Sign out" not in page.text
    overview = await demo.get("/", headers=HTML)
    assert "Two sites need you" in overview.text
    assert "Ridgeback Roofing" in overview.text


async def test_nothing_can_be_changed(demo):
    response = await demo.post(
        "/sites/7/dns-baseline/accept-form", headers={"origin": "http://demo.test"}
    )
    assert response.status_code == 405


async def test_there_is_no_api_and_no_sign_in(demo):
    assert (await demo.get("/sites")).status_code == 404
    assert (await demo.get("/login")).status_code == 404


async def test_the_real_dashboard_still_needs_a_password(showcase_url):
    with pytest.raises(RuntimeError, match="missing required settings"):
        create_app(Settings(database_url=showcase_url))
    app = create_app(Settings(database_url=showcase_url, api_token="t", dashboard_password="p"))
    async with (
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://real.test") as c,
        app.router.lifespan_context(app),
    ):
        page = await c.get("/", headers=HTML)
        assert page.status_code == 303  # to the sign-in page
        assert "Live demo." not in (await c.get("/login")).text


def test_the_daily_rebuild_uploads_a_fresh_showcase(monkeypatch, tmp_path):
    from tideline import aws_lambda

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("TIDELINE_DB_BUCKET", "demo-bucket")
    monkeypatch.setenv("TIDELINE_DB_KEY", "showcase.db")
    monkeypatch.setenv("TIDELINE_SHOWCASE_PACE", "0")
    get_settings.cache_clear()
    with mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        s3.create_bucket(
            Bucket="demo-bucket", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"}
        )
        result = aws_lambda.demo_handler({"task": "showcase"}, None)
        assert result["task"] == "showcase"
        assert s3.head_object(Bucket="demo-bucket", Key="showcase.db")["ContentLength"] > 0
    get_settings.cache_clear()
    Path("/tmp/showcase-build.db").unlink(missing_ok=True)
