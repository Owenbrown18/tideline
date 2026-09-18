# Tideline

**A monitoring service for every website OBdesign runs.** It checks each live client site on the 1st and 15th of every month, notices what has broken or is about to (a lapsing domain, an expiring certificate, a dead contact form), tells Owen, and sends him a report per site to forward to the client.

It is a Python service on AWS Lambda, built as a container image, with Terraform, tests, structured logs, metrics and an alarm. It costs well under USD 1 a month. Its working name was Sitewatch; [docs/brand.md](docs/brand.md) explains the rename.

> **Status (2026-09-18): live on AWS Lambda.** https://status.obwebdesign.ca/ (sign-in required) watches 11 sites with 86 checks across 8 kinds. A full run takes about 21 seconds on Lambda. 376 tests.
>
> **History:** version 1 (2026-09-17) ran every 5 minutes on an always-on EC2 server with Postgres, about USD 20 a month. Owen only needs a twice-monthly check and a monthly report, so on 2026-09-18 it moved to Lambda, a scheduler and a SQLite file in S3 ([decision 0005](docs/decisions/0005-serverless-twice-monthly.md)). The server-era milestones below are kept as they happened.
>
> **See it working:** a public, read-only demo with eight invented businesses at https://tideline.obwebdesign.ca ([decision 0006](docs/decisions/0006-public-demo.md)). Its data is made by the product itself: `tideline showcase` replays six months of real runs against a simulated web.
>
> **Run it locally:** [docs/local-dev.md](docs/local-dev.md). **How the code fits together:** [docs/architecture.md](docs/architecture.md). **Operating it:** [docs/runbook.md](docs/runbook.md).

---

## 1. Why this exists

### The business reason
OBdesign has 11 live client sites and no way to know when one of them breaks. Today a problem is found when a client emails, or by accident. The failures that actually happen to small-business sites are boring and preventable:

- the site goes down, or a deploy leaves a blank page
- an SSL certificate or a domain registration lapses
- a DNS change at the registrar breaks the site or the email (SPF, MX)
- a contact form stops delivering
- a page links somewhere that no longer exists
- a site gets hacked and serves spam (Figs & Honey's old WordPress did exactly this)

Tideline catches all of those. Longer term it is the engine behind a paid maintenance plan: "your site is checked twice a month, and here is your monthly report." Nothing on these sites changes day to day, and a site that goes down is noticed by the client or Owen within hours anyway, so a twice-monthly check catches what matters: the slow failures nobody would notice until too late.

### The career reason
A scan of the UVic co-op board on 2026-09-17 (143 open postings, 43 software developer roles) counted how often each skill appears:

| Skill | Postings | Owen before this project |
|---|---|---|
| APIs | 27 | Uses them, has never shipped his own backend service |
| Python | 25 | Yes, but only local scripts |
| **Cloud (AWS 17, Azure 5, GCP 5)** | **22** | **None** |
| SQL | 19 | Yes |
| Monitoring / observability | 10 | Vercel logs only; no metrics, alarms or dashboards |
| Linux | 10 | Coursework (current lab work over SSH) |
| CI/CD | 10 | Yes |
| **Docker** | **9** | **Coursework only, rusty** |

This project turns the whole cluster into things Owen has actually built and run in production: a Python API and scheduled job, SQL with migrations, Docker images, AWS (Lambda, S3, CloudFront, IAM, SES, CloudWatch, EventBridge), infrastructure as code, CI/CD, and real observability. Version 1 also meant operating a Linux server (patching, disk, backups, restores). It watches real sites for real clients, so it is not a toy.

---

## 2. What it checks

| # | Check | How | Opens an incident when | Milestone |
|---|---|---|---|---|
| 1 | **Uptime and response time** | `GET` the homepage (and any key pages listed for the site); record status code, total time, and TTFB | a failed run (non-2xx/3xx, timeout over 10 s, or connection error). One immediate retry after 30 s before a failure counts. | M1 |
| 2 | **Content sanity** | Page body must contain an expected string (the business name) and must not contain spam markers. Markers are phrases ("online casino", "slot gacor", "canadian pharmacy", "viagra"), not bare words, so a musician's casino gig is not flagged; a site can ignore one with `spam_ignore` (same request as #1) | expected text missing, or a spam marker appears | M1 |
| 3 | **TLS certificate** | Open a TLS connection, read the certificate's expiry and hostname match | under 21 days (warning), under 7 days (critical), or invalid (critical). A connection that fails before the handshake is left to the uptime check. | M1 |
| 4 | **Domain registration** | RDAP lookup for the expiry date (reuse the logic in `~/OBDesign/Systems/leadgen/domain_status.py`) | under 30 days to expiry (warning), under 7 days, expired or a hold/redemption status (critical). RDAP rate limits and outages record nothing. | M1 |
| 5 | **DNS drift** | Resolve A, AAAA, CNAME, MX, NS and TXT for the apex and `www`; compare with the stored baseline. Addresses behind a CNAME are not compared, because a CDN rotates them. | any record differs from the baseline. Owen accepts a change to make it the new baseline (`POST /sites/{id}/dns-baseline/accept`). | M4 |
| 6 | **Email authentication** | Exactly one SPF record, under 10 DNS lookups, DMARC record present | **fail** when mail is actively failing authentication (two SPF records, or over the lookup limit); **warn** for a gap that breaks nothing today (no SPF at all, no DMARC) | M4 |
| 7 | **Broken links** | Crawl the site's internal links (same host, depth limit, polite rate), `HEAD` external links and confirm any failure with a `GET` | **fail** when an internal link returns 4xx/5xx; **warn** for an external 404/410. Bot-blocking answers (Instagram 429, LinkedIn 999) are not broken links, and each broken URL counts once however many pages it appears on. | M5 |
| 8 | **Contact form health** | Find the contact page by following the site's own navigation (or `contact_url` in sites.yaml), confirm a contact form is present, and probe its endpoint with OPTIONS/HEAD/GET. **Never submits the form**: that would email the client, and there is a test that asserts no POST is ever made. | form missing, or its endpoint returns 404/410/5xx | M5 |

Every check runs once per run, on the 1st and 15th at 07:00 Pacific.

Seed list, the 11 live sites (from `Career/Master Source.md`): davesbakery.ca, charliesexcavating.ca, ontheroadside.ca, grainconstruction.ca, nicolconstruction.ca, somavictoria.ca, bayviewcottagesaltspring.com, figsandhoney.com, suzannegaymusic.ca, maidinvictoria.ca, adriennehughes.ca.

**Not in scope:** Lighthouse or performance scoring. Those numbers are internal-only for OBdesign and never go on a résumé, so they are not a reason to build anything.

---

## 3. How it works

```
  EventBridge Scheduler                          Owen's browser
  1st and 15th, 07:00 Pacific                    status.obwebdesign.ca
          │                                              │
          ▼                                              ▼
  ┌───────────────────┐                         CloudFront (HTTPS, certificate)
  │ Lambda            │                                  │
  │ tideline-run      │                                  ▼
  │  load site list   │                         ┌───────────────────┐
  │  run 86 checks ───┼──► client websites      │ Lambda            │
  │  incidents        │                         │ tideline-web      │
  │  daily summary    │                         │  FastAPI dashboard│
  └──┬─────────┬──────┘                         │  + JSON API       │
     │         │  one summary email, only       └─────────┬─────────┘
     │         └► if something changed ─► SES ─► Owen     │
     │            (+ monthly reports on the 1st)          │
     ▼                                                    ▼
  S3: tideline.db (SQLite) ◄──── download / upload ───────┘
      versioned: every run's upload is also a backup

  Secrets and the site list: SSM Parameter Store     Image: ECR (arm64)
  Logs and metrics: CloudWatch     Alarm "the run failed": CloudWatch ─► SNS ─► Owen
  Every resource: Terraform (infra/)
```

**One image, two functions.** The same container image runs as both Lambda functions; only the handler differs.

- **tideline-run**: started by EventBridge Scheduler on the 1st and 15th. Downloads the database from S3, applies any new migration, loads the site list from Parameter Store, runs every check (five at a time), updates incidents, summarises the day into `daily_rollups`, uploads the database back, then emails Owen one summary if anything changed. On the 1st it also emails last month's report for every site.
- **tideline-web**: FastAPI, behind CloudFront. The dashboard is server-rendered (Jinja templates and one stylesheet, no front-end framework and no JavaScript), with a sign-in page. It downloads the database only when it has changed since its last request (S3 ETag). The look, the status shapes and the wording rules are in [docs/brand.md](docs/brand.md).
- **The database** is one SQLite file in S3. Uploads are conditional (S3 If-Match): if the file changed since it was downloaded, the upload is refused rather than silently overwriting it. S3 versioning keeps every earlier copy for 90 days.

### Incident rules
- A check result is `ok`, `warn` or `fail`. An **incident** is a period of non-ok results, with a start, an end and a duration.
- Runs are two weeks apart, so every kind opens an incident on its first bad run. A network blip still cannot open one: the uptime check retries once after 30 s before it counts a failure.
- A run collects every alert it raises (opened, escalated, fixed) into **one** summary email, sent only when something changed. Problems that were already open are listed in that email but never send one on their own, so a quiet run sends nothing.
- A result is `ok`, `warn` (opens a *warning* incident) or `fail` (opens a *critical* one). An incident is dated from the first failure of its streak.
- A check that cannot form an opinion (content on a page that did not load, RDAP rate-limited) records nothing, so one outage is one incident, not three.
- An alert that fails to send is retried on the next run; `alerts` only holds alerts that actually went out.
- Full reasoning: [docs/decisions/0003-incident-and-alert-rules.md](docs/decisions/0003-incident-and-alert-rules.md).
- DNS drift incidents stay open until Owen accepts the new baseline (the button on the site's page, or `POST /sites/{id}/dns-baseline/accept`).
- **Tideline never emails a client.** Alerts and monthly reports go to Owen only; he decides what to forward. This matches the vault's standing rule that nothing contacts clients automatically.

### Data model
```
sites          id, name, domain, urls (json), expected_text, active, created_at
checks         id, site_id, kind, key, interval_seconds, config (json), enabled      (unique site_id, key)
check_results  id, check_id, started_at, duration_ms, status, detail (json)     (indexed on check_id, started_at)
incidents      id, check_id, opened_at, resolved_at, severity, summary, last_alerted_at   (one open per check)
dns_baselines  id, site_id, records (json), accepted_at
daily_rollups  id, site_id, day, results, uptime_checks, uptime_ok, p50_ms, p95_ms, ...   (one per site per run day)
alerts         id, incident_id, channel, sent_at, kind (open|escalated|reminder|resolved)
```
SQLite through SQLAlchemy 2 and Alembic. Times are stored as UTC. A run adds about 90 results; raw results are kept for 400 days and the rollups forever. (`interval_seconds` is from version 1 and no longer drives anything: every enabled check runs once per run.)

### API
```
GET  /healthz                         liveness: process up, DB reachable
GET  /sites                           every site with current status
GET  /sites/{id}                      checks, latest results, open incidents
GET  /sites/{id}/uptime?days=30       uptime % and response-time percentiles
GET  /incidents?open=true
POST /sites/{id}/dns-baseline/accept
GET  /                                 dashboard: overview (HTML)
GET  /sites/{id}/view                  dashboard: one site, its checks and incidents
GET  /incidents/view                   dashboard: open and resolved incidents
GET  /reports                          dashboard: every monthly report
GET  /reports/{site_id}/{yyyy-mm}      monthly report (HTML, also emailed to Owen)
GET  /favicon.svg                      the T. mark, its period in the worst current status
GET  /login, POST /login, GET /logout  the dashboard's sign-in
```
Everything except `/healthz`, `/login` and the stylesheet requires auth: a bearer token for the API, a signed session cookie (from the sign-in page) or basic auth for the dashboard. The dashboard's own "accept DNS" button posts to `/sites/{id}/dns-baseline/accept-form`, which refuses any request that did not come from the dashboard's own origin.

---

## 4. The stack, and why

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, managed with `uv` | The most-asked language on the board (25 postings). Owen already writes it. |
| API | FastAPI + Pydantic, Mangum to run it on Lambda | Typed, async, auto-generated OpenAPI docs. Mangum translates a Lambda request into an ordinary web request, so the app does not know it is on Lambda. |
| HTTP / DNS / TLS | `httpx`, `dnspython`, stdlib `ssl` | Async HTTP with fine-grained timeouts; real DNS queries rather than the system resolver. |
| DB | SQLite (one file in S3), SQLAlchemy 2, Alembic | Tiny data (about 90 rows a run) and one writer at a time: a database server would be all cost and no benefit. Migrations still run through Alembic. |
| Scheduling | EventBridge Scheduler | Starts the run on the 1st and 15th in Pacific time, with retries. Nothing runs, or costs, in between. |
| Tests | pytest, `respx` (HTTP mocking), `moto` (fake AWS), real SQLite | Unit tests for every check and incident rule; integration tests through the real migrations, the Lambda handlers and a real local web server. |
| Quality | `ruff` (lint + format), `mypy --strict` | Enforced in CI. |
| Containers | Docker, AWS's Lambda Python base image, arm64 | The same image runs both functions. Graviton (arm64) is cheaper per second. |
| Cloud | AWS, region `ca-central-1` (Montreal) | AWS is on 17 postings, versus 5 each for Azure and GCP. Canadian region for Canadian clients' data. |
| Compute | Two Lambda functions | See [decision 0005](docs/decisions/0005-serverless-twice-monthly.md). |
| HTTPS | CloudFront + an ACM certificate | A Lambda function URL cannot have a custom domain; CloudFront holds the certificate for `status.obwebdesign.ca` and passes requests through, uncached. |
| Infra as code | Terraform (state in S3) | Every AWS resource is reproducible and reviewable. No clicking around the console. |
| CI/CD | GitHub Actions, AWS access via OIDC; `scripts/deploy.sh` from the laptop | Build, push to ECR, point both functions at the new image, migrate, health-check. No long-lived AWS keys anywhere. |
| Logs | JSON to stdout → CloudWatch Logs | Searchable, structured, one line per check result. |
| Metrics | CloudWatch Embedded Metric Format | `checks_run`, `check_failures`, `check_errors`, `open_incidents` per run, written as log lines that CloudWatch turns into metrics. |
| Alarms | CloudWatch Alarm → SNS email | One alarm: the scheduled run raised an error. A budget alert at USD 3 a month. |
| Email | Amazon SES, domain identity on obwebdesign.ca with DKIM | Run summaries and monthly reports to Owen. The SES sandbox is kept on purpose: it can only mail verified addresses, so it cannot mail a client. |
| Secrets | SSM Parameter Store (SecureString) | API token, dashboard login, and `sites.yaml` itself, read by the functions when they start. Nothing secret in the repo, the image, the function settings or Terraform state. |

### Decision record: from one EC2 server to Lambda
Version 1 was one Graviton EC2 instance running Docker Compose (Caddy, the API, a worker checking every 5 minutes, Postgres), about USD 20 a month. It was the right shape for checking every 5 minutes, and it taught the operations side: a Linux server, disks, backups and a tested restore, a dead-worker alarm measured at 11 min 52 s ([decision 0001](docs/decisions/0001-single-ec2-instance.md), [0004](docs/decisions/0004-heartbeat-alarm.md)).

Owen then set the real requirement: this is a portfolio project that must cost under USD 3 a month, and a check twice a month is plenty. At that cadence an always-on server is idle 99.99% of the time, so the design flipped ([decision 0005](docs/decisions/0005-serverless-twice-monthly.md)):

- **Lambda, not a server:** a run takes about 21 seconds, twice a month. That is well inside Lambda's always-free allowance.
- **SQLite in S3, not Postgres or RDS:** one writer, tiny data. RDS would cost more than the whole budget, and a Lambda talking to RDS needs a VPC, which needs a NAT gateway (about USD 35 a month) to reach the internet it is monitoring.
- **No VPC at all:** the functions only make outbound HTTPS requests, so they stay outside a VPC and reach the internet directly, for free.
- **The trade-off, said plainly:** it no longer notices an outage within minutes. For these sites that is acceptable; for a client paying for uptime monitoring it would not be, and version 1's design (in git history) is the answer.

### Watching the watcher
If a run fails, nobody would know until the monthly report did not arrive. So:
- A CloudWatch alarm emails Owen through SNS when the run function raises an error. SNS does not depend on SES or on Tideline's own code working.
- The monthly report on the 1st is the "it ran" signal: CloudWatch alarms can only look back seven days, and runs are fourteen days apart.
- EventBridge Scheduler retries a failed start twice.

### Security
- Nothing is listening except the dashboard function behind CloudFront, and it requires a sign-in. There is no server to patch or SSH into.
- Least-privilege IAM, one role per function: the run can read and write the one database object, read its own parameters, and send email to Owen only (an IAM condition on the recipient). The dashboard can do the first two, and cannot send email at all. The GitHub OIDC role can push to one ECR repository and update two functions.
- The sign-in sets an HttpOnly, SameSite=Lax cookie signed with a key derived from the password; a wrong password costs a one-second delay.
- Outbound checks send an honest `User-Agent: Tideline/1.0 (+https://obwebdesign.ca)` and respect rate limits. The link crawler stays on the client's own host.
- `docs/threat-model.md`: a short threat model of the service itself (useful practice alongside SENG 360).

---

## 5. Repo layout

```
tideline/
  README.md                 this brief
  pyproject.toml            uv-managed; ruff, mypy, pytest config
  Dockerfile                the Lambda image (arm64), used by both functions
  alembic/                  migrations
  sites.example.yaml        site list format (the real sites.yaml is git-ignored)
  demo/                     fake site + its sites file for scripts/demo.sh
  src/tideline/
    aws_lambda.py           the two Lambda handlers: run, and the dashboard
    api/                    FastAPI app, routes, auth and sign-in, views, templates/, static/
    worker/                 run.py (one whole run), runner.py (one check)
    checks/                 one module per check kind, each a pure function of (config, clients) -> Result
    incidents/              incident state machine + applying its decisions
    notify/                 SES email, the run summary (digest), alert and email templates
    reports/                daily rollups, the monthly report
    db/                     models, session, baselines, store.py (the database file in S3)
    observability/          JSON logging, EMF metrics
    brand.py                Tideline's words, status tones and favicon
    schedule.py             the 1st-and-15th schedule, for the dashboard and reports
    showcase.py             six months of invented businesses, run through the real product
    config.py               settings from the environment
    sites.py                sites.yaml validation and idempotent seeding
    cli.py                  `tideline migrate | seed | run | api | check | rollup | report`
  tests/
    unit/                   checks, incident rules, schedule, summary email, S3 store, report wording
    integration/            API, runs, reports and the Lambda handlers against real SQLite
  infra/                    Terraform: Lambda, scheduler, CloudFront, IAM, ECR, S3, SES, alarm, budget, GitHub OIDC
  scripts/                  deploy.sh, demo.sh, photos.sh, put_secrets.sh (SSM), bootstrap_state.sh, postgres_to_sqlite.py
  docs/
    architecture.md
    brand.md
    local-dev.md            run, demo, test
    runbook.md              deploy, run now, roll back, restore, rotate secrets, accept a DNS change
    threat-model.md
    decisions/              short ADRs
  .github/workflows/
    ci.yml                  ruff, mypy, pytest, the demo, and the image, on every push/PR
    deploy.yml              on main: build, push to ECR, update both functions, migrate, health-check
```

The real site list lives in the database (seeded from a git-ignored `sites.yaml`; `sites.example.yaml` is committed), so the repo can be public without publishing client configuration.

---

## 6. Build plan

Each milestone ends with something working and verified, not "code written". Work in order. M0 to M5 built version 1 (the EC2 server) and are kept as they happened; M6 is the move to Lambda.

**M0: Accounts and tools (done 2026-09-17)**
- Create the AWS account. Turn on MFA for the root user, then stop using root: create an IAM Identity Center admin user.
- Create a **budget alarm at USD 25/month** before anything else.
- Install: Docker (OrbStack or Docker Desktop), `uv`, `terraform`, `awscli`. Create the GitHub repo.
- Done when: `aws sts get-caller-identity` works with the admin user, and `docker run hello-world` works. **Done 2026-09-17.** Account 053578820490; sign in with `aws sso login --profile sitewatch`; portal https://d-9d6748d0db.awsapps.com/start.

**M1: The core, locally** (done 2026-09-17)
- Project skeleton, config, DB models, first Alembic migration.
- Checks 1–4 (uptime, content, TLS, domain), the scheduler, and the incident state machine.
- `sites.yaml` seeded with the 11 sites.
- Unit tests for every check (mocked HTTP) and every incident transition (ok → fail → fail opens → ok resolves, flapping, reminders).
- Done when: `docker compose up` runs locally, results land in Postgres, and killing a fake site's server opens and then resolves an incident in the logs.

**M2: API, containers, CI**
- FastAPI endpoints and auth, `/healthz`, a minimal dashboard.
- Production Dockerfile (multi-stage, non-root), `compose.prod.yaml`.
- `ci.yml`: ruff, mypy, pytest with a real Postgres service container. Green on every push.
- Done when: CI is green, and the image builds for arm64.

**M3: AWS infrastructure and deploys** (done 2026-09-17; push-to-deploy waits on the GitHub account)
- Terraform: security group, EC2 (Amazon Linux 2023, Docker installed by user-data), instance role, ECR, S3 backup bucket, SSM parameters, GitHub OIDC role.
- `deploy.yml`: build, push, SSM deploy, health-check, roll back on failure.
- Caddy serving `status.obwebdesign.ca` over HTTPS (one DNS record at the obwebdesign.ca DNS host). **Done 2026-09-17**: A record at Hostinger to 15.175.12.202, certificate from Let's Encrypt over the http-01 challenge.
- Done when: a push to `main` goes live without touching the server, and a deliberately broken build rolls back by itself. **Rollback verified 2026-09-17** (broken image, health check failed, previous tag restored automatically). The push-to-deploy half needs GitHub Actions enabled on the account.

**M4: Alerts and observability** (done 2026-09-17)
- SES domain identity and DKIM, alert emails (open, reminder, resolved).
- JSON logs to CloudWatch; EMF metrics; CloudWatch agent; heartbeat, disk and backup alarms; a CloudWatch dashboard.
- Checks 5–6 (DNS drift, SPF/DMARC). Baselines captured on first run.
- Nightly `pg_dump` to S3, and **one real restore tested** and written into the runbook.
- Done when: stopping the worker on the server produces the heartbeat alarm email within 15 minutes, and a real restore has been done once. **Verified 2026-09-18**: stopping the worker on the live instance produced the alarm email in 11 min 52 s. Restores: one by hand on 2026-09-17, and `restore.sh` against the first automatic nightly backup on 2026-09-18 (11 sites, 88 checks, 1,737 results).

**M5: Reports and the rest of the checks** (built and deployed 2026-09-18)
- Daily rollups; uptime % and p50/p95 per site; 30-day views on the dashboard.
- Monthly report per site (HTML email to Owen).
- Checks 7–8 (broken links, contact form health).
- Done when: the first monthly report for all 11 sites is in Owen's inbox. **Done 2026-09-18.** Reports are built from daily rollups, so they survive the 90-day purge of raw results, and they are viewable at `/reports/{site_id}/{yyyy-mm}` as well as emailed.

**M6: Pay-per-use** (done 2026-09-18)
- Owen's budget: under USD 3 a month, with checks once or twice a month. Replace the always-on server with two Lambda functions, EventBridge Scheduler (1st and 15th), CloudFront for the domain, and SQLite in S3 instead of Postgres.
- One summary email per run instead of an email per incident; reports and the dashboard reworded for two checks a month.
- A sign-in page, because the browser's basic-auth box cannot work behind a Lambda function URL.
- Production data moved across with every row counted (11 sites, 88 checks, 2,687 results, 28 incidents); the final Postgres backup is kept in S3.
- Done when: a scheduled-style run succeeds on Lambda and the dashboard serves through CloudFront. **Verified 2026-09-18**: 86 checks in 21 s on Lambda.

**Later, only with a reason:** public status pages per client, Slack/SMS alerts, a second probe location, RDS/ECS migration, checks for client Keystatic Cloud or Square integrations, selling it as part of a maintenance plan.

---

## 7. Turning it into résumé facts

The rules from `Career/Resume Rules.md` and `Career/Master Source.md` apply: **nothing goes on a document until it is running and verified, and every number is measured, never estimated.**

- When M3 is live, add a Tideline section to Master Source with the stack and what is deployed, checked against the repo and AWS.
- Numbers worth measuring once it has run for a while, each pulled from the database or CloudWatch with a date: sites monitored, checks per day, test count, real incidents caught (each described from its incident record: what broke, when, how long, what fixed it), deploys shipped through the pipeline, the monitor's own uptime.
- **Real incidents are the interview stories.** Write each one into `docs/incidents/` the week it happens, while the details are fresh.
- **Defensibility:** this will be built with Claude Code, which is fine and should be said plainly if asked. UVic's co-op AI policy draws the line at misrepresentation, so every part of the stack on the résumé has to be explainable unaided: what a Dockerfile layer is, how the OIDC deploy authenticates, why the NAT gateway trap rules out Lambda here, how an incident opens and closes, how a backup is restored. Before each milestone is called done, Owen should be able to walk through it without notes.

A draft résumé line, for once it is true:

> Built and deployed a Python monitoring service (FastAPI, SQL, Docker, AWS Lambda) that checks 11 production client websites for downtime, certificate, domain and DNS failures, with Terraform, CloudWatch alarms and email alerting; cut its AWS cost from about USD 20 a month to under 1 by moving from an EC2 server to Lambda.

(Every number in that line needs checking against the bill and the repo before it is used: see the rule above.)

---

## 8. Cost

**Target: under USD 3 a month. Expected: under USD 1.** Twice-monthly runs sit inside AWS's always-free allowances for Lambda, EventBridge Scheduler, CloudFront, CloudWatch (under 10 metrics and alarms), SNS and SSM Parameter Store. What is left is storage: S3 (the database, about 1 MB, plus its versions) and ECR (the image, kept to the last 3), a few cents, and SES at USD 0.10 per 1,000 emails. A budget alert emails Owen if the month heads past USD 3.

Version 1 (EC2, 2026-09-17 to 18) cost about USD 20 a month: the instance about 13.40, its public IP 3.65, its disks 2.65. September 2026's bill includes about two days of it.

---

## 9. Open decisions (Owen)

1. ~~**Name.**~~ Decided 2026-09-17: **Tideline** (by OBdesign). "Sitewatch" collides with getsitewatch.com, a website-monitoring product for agencies with the same feature set. Brand proposal (logo, deep-sea blue palette, type, screens): https://claude.ai/artifact/V8A2dKs2WJL8Su5XogUFNJ. The code and repo were renamed the same day; some AWS names keep `sitewatch` (docs/brand.md, "The rename").
2. ~~**Public or private repo.**~~ Decided 2026-09-17: public.
3. ~~**Dashboard address.**~~ Decided: `status.obwebdesign.ca`. Since 2026-09-18 a CNAME at Hostinger to CloudFront (see docs/runbook.md); version 1's A record to 15.175.12.202 must not remain, because that IP has been released.
4. ~~**Cost ceiling.**~~ Decided 2026-09-18: under USD 3 a month, checks on the 1st and 15th.
