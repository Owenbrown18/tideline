"""Apply the state machine's decision to the database and send the alert."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tideline.checks.base import Status
from tideline.db.models import Alert, Check, CheckResult, Incident
from tideline.incidents.engine import Action, Decision, OpenIncident, decide, policy_for
from tideline.notify.base import AlertKind, AlertMessage, Notifier
from tideline.observability.logging import log_event

log = logging.getLogger("tideline.incidents")

ALERT_KIND_FOR: dict[Action, AlertKind] = {
    Action.OPEN: "open",
    Action.RETRY_OPEN_ALERT: "open",
    Action.ESCALATE: "escalated",
    Action.REMIND: "reminder",
    Action.RESOLVE: "resolved",
}


async def process_result(
    session: AsyncSession,
    check: Check,
    result: CheckResult,
    summary: str,
    notifier: Notifier,
    now: datetime,
    reminder_every: timedelta = timedelta(hours=24),
) -> Decision:
    """Open, update or resolve this check's incident after `result` was flushed.

    Runs inside the caller's transaction. The open incident row is locked
    (SELECT ... FOR UPDATE) so two overlapping runs cannot both open one; the
    partial unique index on incidents backs that up.
    """
    policy = policy_for(check.kind, reminder_every)

    incident = await session.scalar(
        select(Incident)
        .where(Incident.check_id == check.id, Incident.resolved_at.is_(None))
        .with_for_update()
    )
    rows = (
        await session.execute(
            select(CheckResult.status, CheckResult.started_at)
            .where(CheckResult.check_id == check.id)
            .order_by(CheckResult.started_at.desc(), CheckResult.id.desc())
            .limit(policy.open_after)
        )
    ).all()
    recent: list[Status] = [row.status for row in rows]

    state = (
        OpenIncident(incident.opened_at, incident.severity, incident.last_alerted_at)  # type: ignore[arg-type]
        if incident
        else None
    )
    decision = decide(policy, state, recent, now)

    if decision.action is Action.NONE:
        # Nothing to send, but keep the incident's description current: the
        # reason a check is failing can change while it stays failing (a contact
        # page that 404s, then a form endpoint that 404s).
        if incident is not None and recent and recent[0] != "ok":
            incident.summary = summary
        return decision

    if decision.action is Action.OPEN:
        assert decision.severity is not None
        # The outage began at the first failure of the streak, not at the result
        # that crossed the threshold.
        incident = Incident(
            check_id=check.id,
            opened_at=rows[-1].started_at,
            severity=decision.severity,
            summary=summary,
        )
        session.add(incident)
        await session.flush()
    assert incident is not None

    if decision.action in (Action.ESCALATE, Action.RETRY_OPEN_ALERT):
        assert decision.severity is not None
        incident.severity = decision.severity
    if decision.action in (Action.ESCALATE, Action.REMIND, Action.RETRY_OPEN_ALERT):
        incident.summary = summary
    if decision.action is Action.RESOLVE:
        incident.resolved_at = result.started_at

    log_event(
        log,
        "incident_" + decision.action.value,
        logging.WARNING,
        incident_id=incident.id,
        site=check.site.domain,
        check_kind=check.kind,
        check_key=check.key,
        severity=incident.severity,
        summary=summary if decision.action is not Action.RESOLVE else incident.summary,
    )

    kind = ALERT_KIND_FOR[decision.action]
    message = AlertMessage(
        kind=kind,
        site_name=check.site.name,
        domain=check.site.domain,
        check_kind=check.kind,
        check_key=check.key,
        severity=incident.severity,
        # A resolved alert repeats what the problem was; the email says it is fixed.
        summary=summary if kind != "resolved" else incident.summary,
        opened_at=incident.opened_at,
        resolved_at=incident.resolved_at,
        site_id=check.site.id,
    )
    try:
        await notifier.send(message)
    except Exception:
        # The incident is still recorded. With last_alerted_at unset, an unsent
        # open alert is retried on the next run (Action.RETRY_OPEN_ALERT).
        log.exception("alert_send_failed", extra={"incident_id": incident.id, "alert_kind": kind})
        return decision

    incident.last_alerted_at = now
    session.add(Alert(incident_id=incident.id, channel=notifier.channel, sent_at=now, kind=kind))
    return decision
