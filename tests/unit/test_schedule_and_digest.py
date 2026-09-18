"""The twice-monthly schedule, and the one summary email a run sends."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.test_notify_sites_logging import message
from tideline.notify.digest import Digest, RunSummary, StillOpen, render_html, render_text
from tideline.schedule import is_report_day, next_run

WHEN = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # 07:00 on the 15th in Vancouver


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 9, 17, 12, 0, tzinfo=UTC), datetime(2026, 10, 1, 14, 0, tzinfo=UTC)),
        (datetime(2026, 10, 1, 13, 59, tzinfo=UTC), datetime(2026, 10, 1, 14, 0, tzinfo=UTC)),
        (datetime(2026, 10, 1, 14, 0, tzinfo=UTC), datetime(2026, 10, 15, 14, 0, tzinfo=UTC)),
        # After BC stays on UTC-7 all year (November 2026), 07:00 is still 14:00 UTC.
        (datetime(2026, 12, 20, 0, 0, tzinfo=UTC), datetime(2027, 1, 1, 14, 0, tzinfo=UTC)),
    ],
)
def test_next_run(now, expected):
    assert next_run(now) == expected


def test_reports_go_out_on_the_first_in_pacific_time():
    assert is_report_day(datetime(2026, 10, 1, 14, 0, tzinfo=UTC))
    # 1 October 03:00 UTC is still 30 September in Vancouver.
    assert not is_report_day(datetime(2026, 10, 1, 3, 0, tzinfo=UTC))


class Inbox:
    channel = "test"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, msg) -> None:  # pragma: no cover - the digest never forwards alerts
        raise AssertionError("alerts go into the summary, not straight out")

    async def send_report(self, subject: str, text: str, html: str) -> str:
        self.sent.append((subject, text, html))
        return "id"


async def test_a_quiet_run_sends_nothing():
    inbox = Inbox()
    digest = Digest(inbox)
    assert await digest.flush(WHEN, still_open=[]) is None
    assert inbox.sent == []


async def test_problems_that_were_already_open_do_not_send_on_their_own():
    inbox = Inbox()
    digest = Digest(inbox)
    await digest.send(message(kind="reminder"))
    still = [StillOpen("Figs & Honey", "email_auth", "warning", "No DMARC record", WHEN)]
    assert await digest.flush(WHEN, still) is None
    assert inbox.sent == []


async def test_new_and_fixed_problems_go_out_as_one_email():
    inbox = Inbox()
    digest = Digest(inbox)
    await digest.send(
        message(check_kind="form", summary="The form posts to /api/contact, which returns 404")
    )
    await digest.send(message(site_name="Figs & Honey", kind="resolved", resolved_at=WHEN))
    still = [
        StillOpen(
            "Figs & Honey", "email_auth", "warning", "No DMARC record", WHEN - timedelta(days=30)
        )
    ]
    assert await digest.flush(WHEN, still) == "id"

    [(subject, text, html)] = inbox.sent
    assert subject == "Tideline check: 1 new problem, 1 fixed"
    assert text.startswith("One new problem since the last check.")
    assert "Daves' Bakery's contact form isn't delivering." in text
    assert "What to do: Send one enquiry through the form yourself." in text
    assert "Figs & Honey is back up." in text
    assert "Figs & Honey, email authentication: No DMARC record" in text
    assert "Checked Tuesday 15 September, 07:00 PDT." in text
    assert "#c0432f" in html  # a new critical problem: the red stripe
    assert "Still open" in html


def test_only_fixes_read_as_good_news():
    summary = RunSummary.of([message(kind="resolved", resolved_at=WHEN)], [], WHEN)
    assert summary.subject == "Tideline check: 1 fixed"
    assert summary.headline == "One problem fixed since the last check."
    assert summary.tone == "up"
    assert "—" not in render_text(summary) + render_html(summary)
