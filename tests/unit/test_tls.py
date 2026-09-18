"""TLS check against real TLS servers on localhost, with certificates from a test CA."""

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import trustme

from sitewatch.checks import tls
from sitewatch.checks.tls import evaluate_expiry
from tests.conftest import FIXED_NOW


@pytest.fixture(scope="module")
def ca() -> trustme.CA:
    return trustme.CA()


@asynccontextmanager
async def tls_server(cert: trustme.LeafCert) -> AsyncIterator[int]:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert.configure_cert(context)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()


@pytest.fixture
def client_context(ca: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return context


def site(port: int, hostname: str = "example.ca") -> dict[str, object]:
    return {"domain": hostname, "hostname": hostname, "port": port, "connect_host": "127.0.0.1"}


async def run_against(
    cert: trustme.LeafCert,
    make_clients: Callable[..., object],
    context: ssl.SSLContext,
    **cfg: object,
):
    async with tls_server(cert) as port:
        return await tls.run(
            site(port) | cfg, make_clients(ssl_context=context, now=lambda: datetime.now(UTC))
        )


async def test_valid_certificate_is_ok(ca, make_clients, client_context):
    cert = ca.issue_cert("example.ca", not_after=datetime.now(UTC) + timedelta(days=80))
    result = await run_against(cert, make_clients, client_context)
    assert result.status == "ok"
    assert result.detail["valid"] is True
    assert 79 <= result.detail["days_left"] <= 80


async def test_certificate_expiring_in_ten_days_warns(ca, make_clients, client_context):
    cert = ca.issue_cert("example.ca", not_after=datetime.now(UTC) + timedelta(days=10, hours=1))
    result = await run_against(cert, make_clients, client_context)
    assert result.status == "warn"
    assert "expires in 10 days" in result.summary


async def test_certificate_expiring_in_three_days_is_critical(ca, make_clients, client_context):
    cert = ca.issue_cert("example.ca", not_after=datetime.now(UTC) + timedelta(days=3, hours=1))
    result = await run_against(cert, make_clients, client_context)
    assert result.status == "fail"


async def test_hostname_mismatch_fails(ca, make_clients, client_context):
    cert = ca.issue_cert("some-other-site.ca")
    result = await run_against(cert, make_clients, client_context)
    assert result.status == "fail"
    assert result.detail["valid"] is False
    assert "Hostname mismatch" in result.detail["error"]


async def test_untrusted_certificate_fails(make_clients, client_context):
    stranger = trustme.CA().issue_cert("example.ca")  # signed by a CA the client does not trust
    result = await run_against(stranger, make_clients, client_context)
    assert result.status == "fail"
    assert "Invalid certificate" in result.summary


async def test_expired_certificate_fails(ca, make_clients, client_context):
    cert = ca.issue_cert(
        "example.ca",
        not_before=datetime.now(UTC) - timedelta(days=100),
        not_after=datetime.now(UTC) - timedelta(days=1),
    )
    result = await run_against(cert, make_clients, client_context)
    assert result.status == "fail"
    assert "expired" in result.detail["error"]


async def test_closed_port_gives_no_verdict(make_clients, client_context):
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    assert await tls.run(site(port), make_clients(ssl_context=client_context)) is None


@pytest.mark.parametrize(
    ("days", "status"),
    [(90, "ok"), (21.01, "ok"), (20.9, "warn"), (7.01, "warn"), (6.9, "fail"), (-1, "fail")],
)
def test_expiry_thresholds(days, status):
    assert evaluate_expiry(FIXED_NOW + timedelta(days=days), FIXED_NOW)[0] == status
