"""The incident state machine, as one pure function.

After every check result the worker asks `decide()` what should happen. It
touches no database and sends nothing, so every rule below has a unit test.

States and transitions for one check:

    no incident ──(N non-ok results in a row)──────────► OPEN      alert: open
    open ──(a fail while the incident is only a warning)► OPEN      alert: escalated
    open ──(still non-ok, 24 h since the last alert)────► OPEN      alert: reminder
    open ──(an ok result)───────────────────────────────► RESOLVED  alert: resolved

N is the policy's `open_after`: 2 for uptime (so one blip never pages anyone),
1 for everything else. Flapping (fail, ok, fail, ok) never opens an uptime
incident because the streak resets on every ok.

Checks whose incidents need a human decision (DNS drift, M4) set
`resolve_on_ok=False`: an ok result does not close them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from sitewatch.checks.base import Status

Severity = Literal["warning", "critical"]


class Action(StrEnum):
    NONE = "none"
    OPEN = "open"
    RETRY_OPEN_ALERT = "retry_open_alert"
    ESCALATE = "escalate"
    REMIND = "remind"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class Policy:
    open_after: int = 1
    reminder_every: timedelta = timedelta(hours=24)
    resolve_on_ok: bool = True


@dataclass(frozen=True)
class OpenIncident:
    opened_at: datetime
    severity: Severity
    last_alerted_at: datetime | None


@dataclass(frozen=True)
class Decision:
    action: Action
    severity: Severity | None = None


def severity_of(status: Status) -> Severity | None:
    return {"ok": None, "warn": "warning", "fail": "critical"}[status]


def _worst(statuses: Sequence[Status]) -> Severity:
    return "critical" if "fail" in statuses else "warning"


def decide(
    policy: Policy,
    incident: OpenIncident | None,
    recent: Sequence[Status],
    now: datetime,
) -> Decision:
    """What to do after a new result.

    `recent` is this check's latest statuses, newest first, and must include the
    result just recorded. It needs at least `policy.open_after` entries to open.
    """
    if not recent:
        raise ValueError("recent must include the result just recorded")
    current = recent[0]

    if incident is None:
        if current == "ok":
            return Decision(Action.NONE)
        streak = recent[: policy.open_after]
        if len(streak) == policy.open_after and all(s != "ok" for s in streak):
            return Decision(Action.OPEN, _worst(streak))
        return Decision(Action.NONE)

    if current == "ok" and policy.resolve_on_ok:
        return Decision(Action.RESOLVE, incident.severity)

    if incident.last_alerted_at is None:
        # The open alert never went out (email down, say). Keep trying each run
        # instead of waiting a day for the first reminder.
        severity: Severity = "critical" if current == "fail" else incident.severity
        return Decision(Action.RETRY_OPEN_ALERT, severity)

    if current == "fail" and incident.severity == "warning":
        return Decision(Action.ESCALATE, "critical")

    if now - incident.last_alerted_at >= policy.reminder_every:
        return Decision(Action.REMIND, incident.severity)

    return Decision(Action.NONE, incident.severity)


def policy_for(kind: str, reminder_every: timedelta = timedelta(hours=24)) -> Policy:
    """Per-kind incident rules from README section 2."""
    if kind == "uptime":
        return Policy(open_after=2, reminder_every=reminder_every)
    if kind == "dns":
        return Policy(open_after=1, reminder_every=reminder_every, resolve_on_ok=False)
    return Policy(open_after=1, reminder_every=reminder_every)
