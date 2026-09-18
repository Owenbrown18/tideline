"""Every transition of the incident state machine (README section 3, "Incident rules")."""

from datetime import timedelta

import pytest

from tests.conftest import FIXED_NOW as NOW
from tideline.incidents.engine import Action, OpenIncident, Policy, decide, policy_for

UPTIME = policy_for("uptime")
TLS = policy_for("tls")
DNS = policy_for("dns")
# The engine still supports streaks and reminders; the twice-monthly schedule
# just does not use them. These policies keep that behaviour under test.
STREAK = Policy(open_after=2, reminder_every=timedelta(hours=24))
DNS_REMINDING = policy_for("dns", reminder_every=timedelta(hours=24))


def open_incident(severity="critical", alerted_hours_ago: float | None = 1) -> OpenIncident:
    alerted = None if alerted_hours_ago is None else NOW - timedelta(hours=alerted_hours_ago)
    return OpenIncident(
        opened_at=NOW - timedelta(hours=2), severity=severity, last_alerted_at=alerted
    )


def replay(policy: Policy, statuses: list[str]) -> list[Action]:
    """Feed results oldest-first through decide(), tracking incident state like the service does."""
    history: list[str] = []
    incident: OpenIncident | None = None
    actions = []
    for status in statuses:
        history.insert(0, status)
        decision = decide(policy, incident, history, NOW)
        actions.append(decision.action)
        if decision.action is Action.OPEN:
            incident = OpenIncident(NOW, decision.severity, NOW)
        elif decision.action is Action.RESOLVE:
            incident = None
    return actions


def test_policies_match_the_readme():
    # Runs are two weeks apart, so every kind opens on its first bad result,
    # and there are no reminders: each run's summary lists what is still open.
    assert UPTIME.open_after == 1
    assert TLS.open_after == 1
    assert UPTIME.reminder_every is None
    assert DNS.resolve_on_ok is False


def test_one_uptime_failure_opens_at_twice_monthly_runs():
    decision = decide(UPTIME, None, ["fail"], NOW)
    assert decision.action is Action.OPEN
    assert decision.severity == "critical"


def test_no_reminders_by_default():
    decision = decide(UPTIME, open_incident(alerted_hours_ago=24 * 30), ["fail"], NOW)
    assert decision.action is Action.NONE


def test_ok_with_no_incident_does_nothing():
    assert decide(UPTIME, None, ["ok"], NOW).action is Action.NONE


def test_with_a_streak_policy_one_failure_does_not_open():
    assert decide(STREAK, None, ["fail", "ok"], NOW).action is Action.NONE


def test_with_a_streak_policy_the_first_ever_failure_does_not_open():
    assert decide(STREAK, None, ["fail"], NOW).action is Action.NONE


def test_with_a_streak_policy_two_failures_in_a_row_open_critical():
    decision = decide(STREAK, None, ["fail", "fail"], NOW)
    assert decision.action is Action.OPEN
    assert decision.severity == "critical"


def test_ok_fail_fail_ok_opens_then_resolves():
    assert replay(STREAK, ["ok", "fail", "fail", "ok"]) == [
        Action.NONE,
        Action.NONE,
        Action.OPEN,
        Action.RESOLVE,
    ]


def test_flapping_never_opens_with_a_streak_policy():
    assert set(replay(STREAK, ["fail", "ok"] * 6)) == {Action.NONE}


def test_single_failure_opens_non_uptime_checks():
    decision = decide(TLS, None, ["fail"], NOW)
    assert decision.action is Action.OPEN


def test_warning_opens_with_warning_severity():
    decision = decide(TLS, None, ["warn"], NOW)
    assert decision == decide(TLS, None, ["warn", "ok"], NOW)
    assert decision.severity == "warning"


def test_streak_with_any_fail_is_critical():
    assert decide(STREAK, None, ["warn", "fail"], NOW).severity == "critical"


def test_still_failing_within_24h_sends_nothing():
    decision = decide(STREAK, open_incident(alerted_hours_ago=23.9), ["fail", "fail"], NOW)
    assert decision.action is Action.NONE


def test_still_failing_after_24h_sends_a_reminder():
    decision = decide(STREAK, open_incident(alerted_hours_ago=24), ["fail", "fail"], NOW)
    assert decision.action is Action.REMIND


def test_warning_that_becomes_a_failure_escalates():
    decision = decide(TLS, open_incident(severity="warning"), ["fail"], NOW)
    assert decision.action is Action.ESCALATE
    assert decision.severity == "critical"


def test_critical_that_improves_to_warning_stays_critical_quietly():
    decision = decide(TLS, open_incident(severity="critical"), ["warn"], NOW)
    assert decision.action is Action.NONE


def test_single_ok_resolves():
    decision = decide(UPTIME, open_incident(), ["ok", "fail", "fail"], NOW)
    assert decision.action is Action.RESOLVE


def test_unsent_open_alert_is_retried_next_run():
    decision = decide(UPTIME, open_incident(alerted_hours_ago=None), ["fail", "fail"], NOW)
    assert decision.action is Action.RETRY_OPEN_ALERT


def test_unsent_warning_alert_retries_as_critical_when_it_gets_worse():
    decision = decide(TLS, open_incident("warning", alerted_hours_ago=None), ["fail"], NOW)
    assert decision.action is Action.RETRY_OPEN_ALERT
    assert decision.severity == "critical"


def test_recovery_beats_a_pending_alert_retry():
    decision = decide(UPTIME, open_incident(alerted_hours_ago=None), ["ok"], NOW)
    assert decision.action is Action.RESOLVE


def test_dns_incident_is_not_resolved_by_an_ok_result():
    decision = decide(DNS, open_incident(alerted_hours_ago=1), ["ok"], NOW)
    assert decision.action is Action.NONE


def test_dns_incident_still_reminds_while_waiting_for_acceptance():
    decision = decide(DNS_REMINDING, open_incident(alerted_hours_ago=25), ["ok"], NOW)
    assert decision.action is Action.REMIND


def test_empty_history_is_a_programming_error():
    with pytest.raises(ValueError):
        decide(UPTIME, None, [], NOW)
