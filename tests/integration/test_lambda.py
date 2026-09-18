"""The Lambda entry points (tideline.aws_lambda), end to end.

Fake S3 and SSM (moto), a real SQLite file, a real local web server standing in
for a client site. This is what AWS does on the 1st and 15th, and what happens
when Owen opens the dashboard.
"""

import base64
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from tideline import aws_lambda
from tideline.config import get_settings

REGION = "ca-central-1"
BUCKET = "tideline-lambda-test"
PREFIX = "/tideline-test/"
REPO = Path(__file__).resolve().parents[2]


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<title>Lambda Bakery</title><h1>Lambda Bakery</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def client_site() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture
def aws(monkeypatch, tmp_path, client_site) -> Iterator[None]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    settings = {
        "TIDELINE_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/lambda.db",
        "TIDELINE_DB_BUCKET": BUCKET,
        "TIDELINE_SECRETS_PREFIX": PREFIX,
        "TIDELINE_ALEMBIC_INI": str(REPO / "alembic.ini"),
        "TIDELINE_UPTIME_RETRY_DELAY_SECONDS": "0",
        "TIDELINE_NOTIFY_CHANNEL": "log",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(aws_lambda, "SITES_PATH", tmp_path / "sites.yaml")
    aws_lambda._web.clear()
    get_settings.cache_clear()
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        ssm = boto3.client("ssm", region_name=REGION)
        for name, value in {
            "api_token": "token",
            "dashboard_user": "owen",
            "dashboard_password": "pw",
            "sites_yaml": f"""
sites:
  - name: Lambda Bakery
    domain: lambdabakery.test
    expected_text: Lambda Bakery
    urls: ["{client_site}"]
    checks: {{tls: false, domain: false, dns: false, email_auth: false, links: false, form: false}}
""",
        }.items():
            ssm.put_parameter(Name=PREFIX + name, Value=value, Type="SecureString")
        yield
    aws_lambda._web.clear()
    get_settings.cache_clear()


def web_event(path: str, method: str = "GET", auth: bool = True) -> dict:
    """A Lambda function URL request, as CloudFront passes it on."""
    headers = {"host": "abc.lambda-url.ca-central-1.on.aws"}
    if auth:
        headers["authorization"] = "Basic " + base64.b64encode(b"owen:pw").decode()
    return {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": path, "sourceIp": "1.2.3.4"}},
        "isBase64Encoded": False,
    }


def test_a_scheduled_run_checks_every_site_and_saves_to_s3(aws):
    result = aws_lambda.run_handler({}, None)
    assert result["task"] == "run"
    assert result["checks"] == 2
    assert result["failures"] == 0
    head = boto3.client("s3", region_name=REGION).head_object(Bucket=BUCKET, Key="tideline.db")
    assert head["ContentLength"] > 0


def test_a_run_can_be_narrowed(aws):
    result = aws_lambda.run_handler({"task": "run", "kind": "content"}, None)
    assert result["checks"] == 1


def test_the_dashboard_reads_what_the_run_saved(aws, tmp_path):
    aws_lambda.run_handler({}, None)
    # The dashboard runs in its own container, with its own copy of the file.
    Path(get_settings().database_url.split("///", 1)[1]).unlink()

    assert aws_lambda.web_handler(web_event("/", auth=False), None)["statusCode"] == 401
    page = aws_lambda.web_handler(web_event("/"), None)
    assert page["statusCode"] == 200
    assert "Lambda Bakery" in page["body"]
    assert "Up at" in page["body"]


def test_reading_pages_never_uploads_the_database(aws):
    aws_lambda.run_handler({}, None)
    s3 = boto3.client("s3", region_name=REGION)
    before = s3.head_object(Bucket=BUCKET, Key="tideline.db")["ETag"]
    for path in ("/", "/incidents/view", "/reports"):
        assert aws_lambda.web_handler(web_event(path), None)["statusCode"] == 200
    assert s3.head_object(Bucket=BUCKET, Key="tideline.db")["ETag"] == before


def test_migrate_and_report_tasks(aws):
    assert aws_lambda.run_handler({"task": "migrate"}, None) == {"task": "migrate"}
    aws_lambda.run_handler({}, None)
    assert aws_lambda.run_handler({"task": "report", "month": "2026-09"}, None)["sent"] == 1
    with pytest.raises(ValueError, match="unknown task"):
        aws_lambda.run_handler({"task": "nonsense"}, None)


def test_missing_secrets_stop_the_function(aws):
    boto3.client("ssm", region_name=REGION).delete_parameter(Name=PREFIX + "dashboard_password")
    with pytest.raises(RuntimeError, match="missing SSM parameters"):
        aws_lambda.run_handler({}, None)


def test_a_request_that_writes_is_saved_back_to_s3(aws):
    import sqlite3

    aws_lambda.run_handler({}, None)
    s3 = boto3.client("s3", region_name=REGION)
    before = s3.head_object(Bucket=BUCKET, Key="tideline.db")["ETag"]
    _, store = aws_lambda._web_app()

    def writes(event, context):  # stands in for the "accept DNS change" button
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE sites SET name = 'Renamed Bakery'")
        return {"statusCode": 303, "headers": {}, "body": ""}

    aws_lambda._web["handler"] = writes
    aws_lambda.web_handler(web_event("/", method="POST"), None)
    assert s3.head_object(Bucket=BUCKET, Key="tideline.db")["ETag"] != before
