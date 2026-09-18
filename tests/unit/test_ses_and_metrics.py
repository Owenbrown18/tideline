"""SES alerting and EMF metrics."""

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.unit.test_notify_sites_logging import message
from tideline.config import Settings
from tideline.notify import build_notifier
from tideline.notify.base import LogNotifier
from tideline.observability.metrics import NAMESPACE, emf_document, emit

REGION = "ca-central-1"
SENDER = "Tideline <tideline@obwebdesign.ca>"
OWEN = "owenjosephbrown@gmail.com"


# --- the notifier factory -----------------------------------------------------


def test_log_channel_by_default():
    assert isinstance(build_notifier(Settings()), LogNotifier)


def test_unknown_channel_is_rejected():
    with pytest.raises(ValueError, match="unknown TIDELINE_NOTIFY_CHANNEL"):
        build_notifier(Settings(notify_channel="carrier-pigeon"))


# --- SES ----------------------------------------------------------------------


@pytest.fixture
def aws_credentials(monkeypatch):
    """moto needs credentials present, and must never see real ones."""
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


async def test_ses_sends_one_email_to_owen(aws_credentials):
    from tideline.notify.ses import SesNotifier

    # The context-manager form, not the decorator: decorating an async test
    # leaves the coroutine unawaited.
    with mock_aws():
        ses = boto3.client("ses", region_name=REGION)
        ses.verify_domain_identity(Domain="obwebdesign.ca")

        notifier = SesNotifier(REGION, SENDER, OWEN)
        await notifier.send(message())

        assert ses.get_send_quota()["SentLast24Hours"] == 1
        assert notifier.channel == "ses"


async def test_ses_only_ever_addresses_owen(aws_credentials):
    """Even an alert about a client's site goes to Owen, never to the client."""
    from tideline.notify.ses import SesNotifier

    with mock_aws():
        boto3.client("ses", region_name=REGION).verify_domain_identity(Domain="obwebdesign.ca")
        notifier = SesNotifier(REGION, SENDER, OWEN)

        sent: list[dict] = []
        original = notifier._send

        def record(subject: str, body: str, html: str | None = None) -> str:
            sent.append({"subject": subject, "body": body, "html": html})
            return original(subject, body, html)

        notifier._send = record  # type: ignore[method-assign]
        await notifier.send(message(domain="davesbakery.ca", site_name="Daves' Bakery"))

        assert notifier.recipient == OWEN
        # The client is what the alert is about, never who it goes to.
        assert "Daves' Bakery" in sent[0]["subject"]
        assert "davesbakery.ca" in sent[0]["body"]
        # Sent as HTML with the plain text as the fallback.
        assert sent[0]["html"] is not None
        assert "davesbakery.ca" in sent[0]["html"]


async def test_ses_failure_raises_so_the_alert_is_retried(aws_credentials):
    from tideline.notify.ses import SesNotifier

    with mock_aws():
        # No verified identity, so SES refuses the send.
        notifier = SesNotifier(REGION, SENDER, OWEN)
        with pytest.raises(ClientError):
            await notifier.send(message())


# --- EMF metrics ---------------------------------------------------------------


def test_emf_document_shape():
    doc = emf_document({"checks_run": 12, "check_duration_ms": 340}, timestamp_ms=1_700_000_000_000)
    metadata = doc["_aws"]["CloudWatchMetrics"][0]
    assert doc["_aws"]["Timestamp"] == 1_700_000_000_000
    assert metadata["Namespace"] == NAMESPACE
    assert metadata["Dimensions"] == [[]]  # no dimensions: see metrics.py on cost
    assert {m["Name"]: m["Unit"] for m in metadata["Metrics"]} == {
        "checks_run": "Count",
        "check_duration_ms": "Milliseconds",
    }
    assert doc["checks_run"] == 12


def test_emit_writes_one_json_line(capsys):
    emit({"checks_run": 86, "open_incidents": 3})
    line = capsys.readouterr().out.strip()
    parsed = json.loads(line)
    assert parsed["checks_run"] == 86
    assert parsed["open_incidents"] == 3
    assert parsed["event"] == "metrics"


def test_emit_ignores_an_empty_batch(capsys):
    emit({})
    assert capsys.readouterr().out == ""
