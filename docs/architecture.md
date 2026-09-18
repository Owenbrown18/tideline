# Architecture

The brief (README section 3) has the full picture. This page describes the code
as built, and is updated at the end of each milestone.

## A check result's journey (M1)

```
APScheduler job "check:<id>" fires on its interval
  └─ Runner.run_check(check_id)                      worker/runner.py
       ├─ load the check and its site                 (short DB session, closed again)
       ├─ REGISTRY[kind](config, clients)             checks/*.py, no DB access
       │     └─ returns Result(status, summary, detail) or None (no verdict)
       ├─ in one transaction:
       │     ├─ INSERT check_results row
       │     └─ process_result()                      incidents/service.py
       │           ├─ SELECT open incident FOR UPDATE, last N statuses
       │           ├─ decide(policy, incident, statuses, now)   incidents/engine.py (pure)
       │           ├─ open / escalate / remind / resolve the incident row
       │           └─ notifier.send(alert); INSERT alerts row only if it was sent
       └─ log one JSON line: event=check_result
```

## Why the pieces are split this way
- **Checks are pure functions of (config, clients).** Everything that touches
  the network comes in through `Clients`, so tests swap in a mocked HTTP
  client, a test certificate authority or a fake clock. No check knows the
  database exists.
- **The incident rules are one pure function** (`decide`). Every transition in
  the README has a unit test without a database. The service around it only
  loads state and saves the decision.
- **"No verdict" is different from "ok".** A content check on a page that did
  not load, a TLS check that could not connect, or an RDAP lookup that was rate
  limited returns `None`: nothing is stored and no incident changes. The failure
  belongs to the check that owns it (uptime), so one outage is one incident.
- **Two checks, one request.** Uptime and content run on the same interval
  against the same URL. `PageFetcher` remembers each page for 60 s, so whichever
  runs second reuses the first one's response (README: "same request as #1").
  The uptime retry always makes a fresh request.
- **The database enforces the invariants too.** A partial unique index allows
  at most one open incident per check; check constraints restrict `kind`,
  `status`, `severity` and alert `kind` to known values.

## Worker lifecycle
- On start: schedule one job per enabled check of an active site. First runs are
  spread over the first minute (`first_run_offset`).
- Every 60 s: reconcile jobs with the `checks` table (added, removed, interval changed).
- Every 60 s: `worker_heartbeat` log line with counts since the last one. M4 turns
  this into the CloudWatch metric the "watching the watcher" alarm uses.
- `max_instances=1` per job: a slow run is never overlapped by the next.
- SIGTERM (`docker stop`) stops the scheduler and closes connections cleanly.

## Observability (M4)

```
worker / api containers
  │  JSON log lines on stdout, one per check result
  ├──► Docker awslogs driver ──► CloudWatch Logs /sitewatch/containers (30-day retention)
  │        └── Logs Insights queries per site, per check, per status
  └──► EMF metric lines ───────► CloudWatch metrics, namespace Tideline
                                   worker_heartbeat, checks_run, check_failures,
                                   open_incidents, check_duration_ms, backup_success
                                     │
CloudWatch agent (host) ──► CWAgent: mem_used_percent, disk_used_percent
                                     │
                                     ▼
                              5 CloudWatch alarms ──► SNS topic ──► Owen's email
```

**Why metrics as log lines (EMF).** A metric published through `PutMetricData`
needs credentials, a network call and error handling in the app. EMF is a
specially shaped JSON line: Docker already ships stdout to CloudWatch Logs, and
CloudWatch turns the line into a metric on ingestion. One code path, no extra
failure mode, and the line is still readable in the logs locally where there is
no CloudWatch at all.

**No per-site metric dimensions.** CloudWatch charges per metric per month, and
11 sites times 5 metrics would cost more than the server. Aggregate metrics
drive the alarms; per-site detail comes from the logs, which are already there.

**Two alert paths.** Incidents go out over SES from the app. Alarms (dead
worker, full disk, failed backup) go over CloudWatch to SNS, which does not
depend on the instance or on Tideline working. If the box dies, the alarm
still arrives.

**The heartbeat alarm treats missing data as breaching.** This is the whole
point of watching the watcher: a dead worker publishes nothing, and the
CloudWatch default (ignore missing data) would leave the alarm green forever.
