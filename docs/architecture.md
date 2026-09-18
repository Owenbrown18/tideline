# Architecture

The brief (README section 3) has the full picture. This page describes the code
as built. Version 1 (an EC2 server checking every 5 minutes) is in git history
before 2026-09-18; decision 0005 explains the move.

## One run

```
EventBridge Scheduler, 1st and 15th, 07:00 Pacific
  └─ Lambda tideline-run: run_handler({"task": "run"})      aws_lambda.py
       ├─ read secrets and sites.yaml from SSM Parameter Store
       ├─ download tideline.db from S3                        db/store.py
       ├─ alembic upgrade head (usually nothing to do)
       └─ run(settings, sites_file)                            worker/run.py
            ├─ seed(sites.yaml): add, update, retire checks    sites.py
            ├─ every enabled check, five at a time:
            │    Runner.run_check(check_id)                    worker/runner.py
            │      ├─ REGISTRY[kind](config, clients)          checks/*.py, no DB access
            │      │     └─ Result(status, summary, detail), or None (no verdict)
            │      └─ one short write (writes take turns: SQLite has one writer)
            │           ├─ INSERT check_results row
            │           └─ process_result()                    incidents/service.py
            │                ├─ decide(policy, incident, statuses, now)  incidents/engine.py (pure)
            │                ├─ open / escalate / resolve the incident row
            │                └─ digest.send(alert); INSERT alerts row
            ├─ after_run(): summarise today into daily_rollups, purge results over 400 days
            ├─ digest.flush(): one email to Owen, only if something changed   notify/digest.py
            └─ on the 1st: send_month() emails each site's report             reports/monthly.py
       └─ upload tideline.db to S3, only if nobody changed it meanwhile (If-Match)
```

## One page view

```
browser ─► CloudFront (status.obwebdesign.ca, HTTPS) ─► Lambda function URL
  └─ web_handler(event)                                       aws_lambda.py
       ├─ first request in this container: secrets from SSM, build the app
       ├─ store.download(): only transfers when the S3 ETag changed
       ├─ Mangum(app)(event): an ordinary FastAPI request       api/app.py
       │     ├─ require_dashboard_user: session cookie or basic auth    api/auth.py
       │     └─ views.overview(session) → index.html                   api/views.py
       └─ if the request changed data (POST): upload tideline.db (If-Match)
```

## Why the pieces are split this way
- **Checks are pure functions of (config, clients).** Everything that touches
  the network comes in through `Clients`, so tests swap in a mocked HTTP
  client, a test certificate authority or a fake clock. No check knows the
  database exists.
- **The incident rules are one pure function** (`decide`). Every transition has
  a unit test without a database. The service around it only loads state and
  saves the decision.
- **"No verdict" is different from "ok".** A content check on a page that did
  not load, a TLS check that could not connect, or an RDAP lookup that was rate
  limited returns `None`: nothing is stored and no incident changes. The failure
  belongs to the check that owns it (uptime), so one outage is one incident.
- **Two checks, one request.** Uptime and content check the same URL.
  `PageFetcher` remembers each page for 60 s, so whichever runs second reuses
  the first one's response. The uptime retry always makes a fresh request.
- **The run does not know it is on Lambda.** `worker/run.py` takes settings and
  a notifier; `aws_lambda.py` is the thin layer that fetches secrets and moves
  the database file. `tideline run` on the laptop calls the same function.
- **The database enforces the invariants too.** A partial unique index allows
  at most one open incident per check; check constraints restrict `kind`,
  `status`, `severity` and alert `kind` to known values.

## The database file
- **SQLite, one file, in S3.** Lambda has no disk that survives between
  invocations, so each function works on a copy in `/tmp` and S3 holds the real
  one. At about 90 rows a run and one writer, this is enough, and it costs
  nothing to keep.
- **No lost writes.** Every upload says "only if the file is still the version
  I downloaded" (S3 If-Match). If the dashboard accepted a DNS change while a
  run was going, the run's upload is refused, the function errors, and the
  "run failed" alarm emails Owen. Nothing is silently overwritten.
- **Backups for free.** Bucket versioning keeps each earlier upload for 90 days;
  any of them can be restored from the S3 console (docs/runbook.md).
- **Time zones.** SQLite has none, so `UTCDateTime` stores naive UTC and hands
  back aware datetimes. Nothing else in the code has to think about it.

## Observability

```
both functions: JSON log lines on stdout, one per check result
  ├──► CloudWatch Logs /aws/lambda/tideline-run (90 days), tideline-web (30 days)
  └──► EMF metric lines ──► CloudWatch metrics, namespace Tideline
                              checks_run, check_failures, check_errors, open_incidents

AWS/Lambda Errors for tideline-run ──► alarm "tideline-run-failed" ──► SNS ──► Owen
```

**Why metrics as log lines (EMF).** A metric published through `PutMetricData`
needs credentials, a network call and error handling in the app. EMF is a
specially shaped JSON line: Lambda already ships stdout to CloudWatch Logs, and
CloudWatch turns the line into a metric on ingestion. One code path, no extra
failure mode, and the line is still readable in the logs locally.

**Two alert paths.** Run summaries and reports go out over SES from the app.
The "run failed" alarm goes over CloudWatch to SNS, which does not depend on
Tideline's code working.
