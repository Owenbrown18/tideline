from datetime import timedelta

import httpx
import pytest
import respx

from sitewatch.checks import domain
from sitewatch.checks.domain import evaluate, parse_dt, parse_rdap
from tests.conftest import FIXED_NOW

RDAP = "https://rdap.test/domain/"


def rdap_payload(expiry: str | None = "2027-08-29T04:00:00Z", statuses=("active",)) -> dict:
    """Shaped like a real CIRA (.ca) RDAP answer, trimmed."""
    events = [{"eventAction": "registration", "eventDate": "2020-08-29T04:00:00Z"}]
    if expiry:
        events.append({"eventAction": "expiration", "eventDate": expiry})
    return {
        "objectClassName": "domain",
        "ldhName": "example.ca",
        "status": list(statuses),
        "events": events,
        "entities": [
            {
                "roles": ["registrar"],
                "handle": "123",
                "vcardArray": [
                    "vcard",
                    [["version", {}, "text", "4.0"], ["fn", {}, "text", "Webnames.ca Inc."]],
                ],
            }
        ],
    }


def test_parse_rdap_reads_registrar_expiry_and_status():
    info = parse_rdap(rdap_payload())
    assert info.registrar == "Webnames.ca Inc."
    assert info.expiry.isoformat() == "2027-08-29T04:00:00+00:00"
    assert info.statuses == ["active"]
    assert info.lapsed_reason == ""


def test_registrar_falls_back_to_handle():
    payload = rdap_payload()
    payload["entities"][0].pop("vcardArray")
    assert parse_rdap(payload).registrar == "123"


@pytest.mark.parametrize(
    "status", ["client hold", "serverHold", "redemption period", "pendingDelete"]
)
def test_hold_and_lapsed_statuses_fail(status):
    result = evaluate("example.ca", parse_rdap(rdap_payload(statuses=[status])), FIXED_NOW)
    assert result.status == "fail"


@pytest.mark.parametrize(
    ("days", "status"),
    [(400, "ok"), (30, "ok"), (29, "warn"), (7, "warn"), (6, "fail"), (-2, "fail")],
)
def test_expiry_thresholds(days, status):
    expiry = (FIXED_NOW + timedelta(days=days, hours=1)).isoformat()
    assert evaluate("example.ca", parse_rdap(rdap_payload(expiry)), FIXED_NOW).status == status


def test_no_expiry_published_is_ok_but_says_so():
    result = evaluate("example.ca", parse_rdap(rdap_payload(expiry=None)), FIXED_NOW)
    assert result.status == "ok"
    assert "no expiry date" in result.summary


@pytest.mark.parametrize(
    "value",
    ["2027-08-29T04:00:00Z", "2027-08-29T04:00:00+00:00", "2027-08-29 04:00:00", "2027-08-29"],
)
def test_parse_dt_formats(value):
    assert parse_dt(value).date().isoformat() == "2027-08-29"


def test_parse_dt_rejects_junk():
    assert parse_dt("not a date") is None
    assert parse_dt(None) is None


@respx.mock
async def test_run_queries_rdap(make_clients):
    route = respx.get(RDAP + "example.ca").respond(200, json=rdap_payload())
    result = await domain.run({"domain": "example.ca"}, make_clients())
    assert result.status == "ok"
    assert route.calls.last.request.headers["accept"] == "application/rdap+json"
    assert result.detail["days_left"] == 345


@respx.mock
async def test_unregistered_domain_warns(make_clients):
    respx.get(RDAP + "example.ca").respond(404)
    result = await domain.run({"domain": "example.ca"}, make_clients())
    assert result.status == "warn"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429),
        httpx.Response(503),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
@respx.mock
async def test_rate_limits_and_garbage_give_no_verdict(make_clients, response):
    respx.get(RDAP + "example.ca").mock(return_value=response)
    assert await domain.run({"domain": "example.ca"}, make_clients()) is None


@respx.mock
async def test_network_error_gives_no_verdict(make_clients):
    respx.get(RDAP + "example.ca").mock(side_effect=httpx.ConnectTimeout("slow"))
    assert await domain.run({"domain": "example.ca"}, make_clients()) is None
