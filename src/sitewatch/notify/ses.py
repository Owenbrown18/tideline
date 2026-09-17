"""Alert emails through Amazon SES.

Every alert goes to one address: Owen's. That is enforced three times over, so
a bug, a bad config value or a compromised container still cannot email a
client:

1. this class sends only to `settings.alert_email`, ignoring anything else;
2. the instance's IAM policy allows `ses:SendEmail` only when the recipient is
   that address (`ses:Recipients` condition in infra/iam_instance.tf);
3. SES stays in its sandbox, where only verified addresses can be reached at all.

Sitewatch holds no client email addresses anywhere in its database or config.

boto3 is synchronous, so the call runs in a worker thread and does not block
the event loop that is busy checking sites.
"""

import asyncio
import logging
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from sitewatch.notify.base import AlertMessage, render_body, render_subject
from sitewatch.observability.logging import log_event

log = logging.getLogger("sitewatch.notify.ses")


class SesNotifier:
    channel = "ses"

    def __init__(self, region: str, sender: str, recipient: str) -> None:
        self.sender = sender
        self.recipient = recipient
        # Two quick retries: SES throttling or a blip should not lose an alert,
        # and the incident engine retries an unsent "open" alert anyway.
        self._client: Any = boto3.client(
            "ses",
            region_name=region,
            config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10),
        )

    def _send(self, subject: str, body: str, html: str | None = None) -> str:
        content: dict[str, Any] = {"Text": {"Data": body, "Charset": "UTF-8"}}
        if html is not None:
            content["Html"] = {"Data": html, "Charset": "UTF-8"}
        response = self._client.send_email(
            Source=self.sender,
            Destination={"ToAddresses": [self.recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": content,
            },
        )
        message_id: str = response["MessageId"]
        return message_id

    async def send_report(self, subject: str, text: str, html: str) -> str:
        """Send a monthly report: same one recipient, HTML with a text fallback."""
        message_id = await asyncio.to_thread(self._send, subject, text, html)
        log_event(log, "report_emailed", logging.INFO, subject=subject, message_id=message_id)
        return message_id

    async def send(self, msg: AlertMessage) -> None:
        subject = render_subject(msg)
        body = render_body(msg)
        try:
            message_id = await asyncio.to_thread(self._send, subject, body)
        except (ClientError, BotoCoreError) as exc:
            # Raising means the incident is still recorded and the alert is
            # retried on the next run (incidents/engine.py).
            log_event(
                log,
                "ses_send_failed",
                logging.ERROR,
                alert_kind=msg.kind,
                site=msg.domain,
                error=str(exc)[:200],
            )
            raise
        log_event(
            log,
            "alert_emailed",
            logging.WARNING,
            alert_kind=msg.kind,
            site=msg.domain,
            check_kind=msg.check_kind,
            severity=msg.severity,
            subject=subject,
            message_id=message_id,
        )
