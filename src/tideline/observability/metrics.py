"""CloudWatch metrics, published as log lines (Embedded Metric Format).

A normal metric needs an API call (`PutMetricData`), which means credentials, a
network round trip and a failure mode. EMF avoids all three: the app writes a
specially shaped JSON line to stdout, Docker ships it to CloudWatch Logs, and
CloudWatch turns it into a metric on ingestion. The line is still a readable log
line, so nothing is lost locally, where there is no CloudWatch at all.

Metrics published (namespace `Sitewatch`):

| Metric              | Where from                                    |
|---------------------|-----------------------------------------------|
| `worker_heartbeat`  | the worker, every cycle: the "is it alive" signal |
| `checks_run`        | checks completed since the last heartbeat     |
| `check_failures`    | non-ok results since the last heartbeat       |
| `open_incidents`    | count from the database at each heartbeat     |
| `check_duration_ms` | one value per check, aggregated by CloudWatch |

Deliberately no per-site dimension. CloudWatch charges per metric per month,
and 11 sites times 5 metrics would cost more than the server. Per-site detail
lives in the logs, where Logs Insights can query it for nothing.
"""

import json
import logging
import sys
import time
from typing import Any

log = logging.getLogger("tideline.metrics")

# Still the pre-rename name: the instance's IAM policy and the alarms point at
# it (infra/iam_instance.tf, infra/alarms.tf), so it changes with them.
NAMESPACE = "Sitewatch"

UNITS = {
    "worker_heartbeat": "Count",
    "checks_run": "Count",
    "check_failures": "Count",
    "checks_skipped": "Count",
    "check_errors": "Count",
    "open_incidents": "Count",
    "check_duration_ms": "Milliseconds",
}


def emf_document(values: dict[str, float], timestamp_ms: int | None = None) -> dict[str, Any]:
    """Build the EMF document CloudWatch reads metrics out of."""
    return {
        "_aws": {
            "Timestamp": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [[]],  # aggregate only: see the note above
                    "Metrics": [{"Name": name, "Unit": UNITS.get(name, "None")} for name in values],
                }
            ],
        },
        "event": "metrics",
        **values,
    }


def emit(values: dict[str, float], timestamp_ms: int | None = None) -> None:
    """Write one metrics line. Never raises: monitoring must not break the monitor."""
    if not values:
        return
    try:
        # Straight to stdout rather than through logging: the EMF document is the
        # whole line, with no level or logger fields wrapped around it.
        sys.stdout.write(json.dumps(emf_document(values, timestamp_ms)) + "\n")
        sys.stdout.flush()
    except Exception:  # pragma: no cover - defensive
        log.exception("metrics_emit_failed")
