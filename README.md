# Sitewatch

**A monitoring service for every website OBdesign runs.** It checks each live client site around the clock, notices when something breaks or is about to break, tells Owen before the client notices, and keeps the history that proves the sites are healthy.

It is a Python backend running in Docker on AWS, deployed by GitHub Actions, with tests, structured logs, metrics and alarms. The name is a working name.

> **Status (2026-09-17):** M1 code complete and verified outside Docker (118 tests; a live worker run against all 11 sites plus a killed-and-restarted fake site opened and resolved an incident). M1 is signed off once `docker compose up` is run after M0 installs Docker. M0 (AWS account, tools) is with Owen. This file is the spec: build against it, and update it when a decision changes.
>
> **Run it locally:** [docs/local-dev.md](docs/local-dev.md). **How the code fits together:** [docs/architecture.md](docs/architecture.md).

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

Sitewatch catches all of those. Longer term it is the engine behind a paid maintenance plan: "your site is watched every five minutes, and here is your monthly report."

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

This project turns the whole cluster into things Owen has actually built and run in production: a Python API and worker, Postgres, Docker in production, AWS, a Linux server he operates, CI/CD with automated deploys, and real observability. It watches real sites for real clients, so it is not a toy.

---

## 2. What it checks

| # | Check | How | How often | Opens an incident when | Milestone |
|---|---|---|---|---|---|
| 1 | **Uptime and response time** | `GET` the homepage (and any key pages listed for the site); record status code, total time, and TTFB | every 5 min | 2 failures in a row (non-2xx/3xx, timeout over 10 s, or connection error). One immediate retry after 30 s before a failure counts. | M1 |
| 2 | **Content sanity** | Page body must contain an expected string (the business name) and must not contain spam markers. Markers are phrases ("online casino", "slot gacor", "canadian pharmacy", "viagra"), not bare words, so a musician's casino gig is not flagged; a site can ignore one with `spam_ignore` | every 5 min, same request as #1 | expected text missing, or a spam marker appears | M1 |
| 3 | **TLS certificate** | Open a TLS connection, read the certificate's expiry and hostname match | every 6 h | under 21 days (warning), under 7 days (critical), or invalid (critical). A connection that fails before the handshake is left to the uptime check. | M1 |
| 4 | **Domain registration** | RDAP lookup for the expiry date (reuse the logic in `~/OBDesign/Systems/leadgen/domain_status.py`) | daily | under 30 days to expiry (warning), under 7 days, expired or a hold/redemption status (critical). RDAP rate limits and outages record nothing. | M1 |
| 5 | **DNS drift** | Resolve A, AAAA, CNAME, MX, NS and TXT for the apex and `www`; compare with the stored baseline | hourly | any record differs from the baseline. Owen accepts a change to make it the new baseline. | M4 |
| 6 | **Email authentication** | Exactly one SPF record, under 10 DNS lookups, DMARC record present | daily | SPF missing, duplicated or over the lookup limit, or DMARC missing | M4 |
| 7 | **Broken links** | Crawl the site's internal links (same host, depth limit, polite rate), `HEAD` external links | daily | any internal link returns 4xx/5xx | M5 |
| 8 | **Contact form health** | Load the contact page and confirm the form and its action endpoint (Formspree etc.) are present and reachable. **Never submit a real form**: that would email the client. | daily | form missing or endpoint unreachable | M5 |

Seed list, the 11 live sites (from `Career/Master Source.md`): davesbakery.ca, charliesexcavating.ca, ontheroadside.ca, grainconstruction.ca, nicolconstruction.ca, somavictoria.ca, bayviewcottagesaltspring.com, figsandhoney.com, suzannegaymusic.ca, maidinvictoria.ca, adriennehughes.ca.

**Not in scope:** Lighthouse or performance scoring. Those numbers are internal-only for OBdesign and never go on a résumé, so they are not a reason to build anything.

---

## 3. How it works

```
                         GitHub
             push ─► Actions: lint, test, build image
                          │  (OIDC, no stored AWS keys)
                          ▼
                   Amazon ECR (image)          Terraform: all AWS resources as code
                          │
            SSM Run Command: pull + migrate + restart
                          ▼
 ┌─────────────────── EC2 (Graviton, Amazon Linux 2023) ────────────────────┐
 │  docker compose                                                           │
 │   ┌─────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐   │
 │   │  caddy  │──►│  api          │   │  worker       │   │  postgres     │   │
 │   │  HTTPS  │   │  FastAPI      │   │  scheduler +  │──►│  (EBS volume) │   │
 │   └─────────┘   │  dashboard    │──►│  checks       │   └───────────────┘   │
 │                 └──────────────┘   └──────┬───────┘          │ nightly     │
 └─────────────────────────────────────────────┼──────────────────┼────────────┘
                                               │                  ▼
            JSON logs + metrics ◄──────────────┤            S3 (pg_dump backups)
            CloudWatch Logs / Metrics / Alarms │
                     │                         ▼
                     └──► SNS ──► Owen    SES email alerts ──► Owen
```

**Two processes, one image.** The same Docker image runs as `api` or `worker` depending on its command.

- **worker**: a scheduler (APScheduler) that runs each check on its interval with a concurrency limit. Every result is written to Postgres. After each result, the incident engine decides whether an incident opens, stays open or resolves, and sends alerts. It emits a heartbeat metric every cycle.
- **api**: FastAPI. JSON endpoints plus a small server-rendered dashboard (Jinja templates, no front-end framework: this project is about the backend). Behind Caddy, which handles HTTPS automatically.
- **postgres**: Postgres 16 in a container on the instance's EBS volume, dumped nightly to S3 with 30-day retention. A backup that has never been restored is not a backup, so the restore is tested and written up in `docs/runbook.md`.

### Incident rules
- A check result is `ok`, `warn` or `fail`. An **incident** is a period of non-ok results, with a start, an end and a duration.
- Uptime needs 2 consecutive failures to open an incident (about 10 minutes), which stops one network blip from paging anyone.
- An incident sends **one** alert when it opens, a reminder every 24 h while it stays open, and a recovery notice with the duration when it closes. A warning incident that becomes critical sends one `escalated` alert.
- A result is `ok`, `warn` (opens a *warning* incident) or `fail` (opens a *critical* one). An incident is dated from the first failure of its streak.
- A check that cannot form an opinion (content on a page that did not load, RDAP rate-limited) records nothing, so one outage is one incident, not three.
- An alert that fails to send is retried on the next run; `alerts` only holds alerts that actually went out.
- Full reasoning: [docs/decisions/0003-incident-and-alert-rules.md](docs/decisions/0003-incident-and-alert-rules.md).
- DNS drift incidents stay open until Owen accepts the new baseline (`POST /sites/{id}/dns-baseline/accept`, or the CLI).
- **Sitewatch never emails a client.** Alerts and monthly reports go to Owen only; he decides what to forward. This matches the vault's standing rule that nothing contacts clients automatically.

### Data model (first cut)
```
sites          id, name, domain, urls[], expected_text, active, created_at
checks         id, site_id, kind, key, interval_seconds, config jsonb, enabled      (unique site_id, key)
check_results  id, check_id, started_at, duration_ms, status, detail jsonb     (indexed on check_id, started_at)
incidents      id, check_id, opened_at, resolved_at, severity, summary, last_alerted_at   (one open per check)
dns_baselines  id, site_id, records jsonb, accepted_at
alerts         id, incident_id, channel, sent_at, kind (open|escalated|reminder|resolved)
```
Migrations with Alembic. `check_results` grows by about 3,500 rows a day at 11 sites; keep 90 days of raw results and a daily rollup table (uptime %, p50/p95 response time) forever.

### API (first cut)
```
GET  /healthz                         liveness: process up, DB reachable
GET  /sites                           every site with current status
GET  /sites/{id}                      checks, latest results, open incidents
GET  /sites/{id}/uptime?days=30       uptime % and response-time percentiles
GET  /incidents?open=true
POST /sites/{id}/dns-baseline/accept
GET  /                                 dashboard (HTML)
GET  /reports/{site_id}/{yyyy-mm}      monthly report (HTML, also emailed to Owen)
```
Everything except `/healthz` requires auth (a bearer token for the API, basic auth for the dashboard to start).

---

## 4. The stack, and why

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, managed with `uv` | The most-asked language on the board (25 postings). Owen already writes it. |
| API | FastAPI + Pydantic | Typed, async, auto-generated OpenAPI docs. The standard for new Python services. |
| HTTP / DNS / TLS | `httpx`, `dnspython`, stdlib `ssl` | Async HTTP with fine-grained timeouts; real DNS queries rather than the system resolver. |
| DB | Postgres 16, SQLAlchemy 2, Alembic | SQL is on 19 postings; migrations are how teams change schemas safely. |
| Scheduling | APScheduler in the worker | Simple, in-process, fine at this scale. |
| Tests | pytest, `respx` (HTTP mocking), real Postgres in CI | Unit tests for every check and every incident rule; integration tests against a real database. |
| Quality | `ruff` (lint + format), `mypy` | Enforced in CI. |
| Containers | Docker, `docker compose` for local dev and production | Same images run on the laptop and in AWS. arm64 builds for Graviton. |
| Cloud | AWS, region `ca-central-1` (Montreal) | AWS is on 17 postings, versus 5 each for Azure and GCP. Canadian region for Canadian clients' data. |
| Compute | One EC2 `t4g.small` running compose | See the decision record below. |
| Infra as code | Terraform (state in S3) | Every AWS resource is reproducible and reviewable. No clicking around the console. |
| CI/CD | GitHub Actions, AWS access via OIDC | Lint and test on every push; build, push to ECR and deploy on `main`. No long-lived AWS keys anywhere. |
| Deploy | SSM Run Command | No SSH port open. Deploy script: pull, `alembic upgrade head`, restart, poll `/healthz`, roll back to the previous image tag on failure. |
| TLS / proxy | Caddy | Automatic Let's Encrypt certificates for `status.obwebdesign.ca`. |
| Logs | JSON to stdout → Docker `awslogs` driver → CloudWatch Logs | Searchable, structured, one line per check result. |
| Metrics | CloudWatch Embedded Metric Format from the app; CloudWatch agent for host CPU, memory, disk | `checks_run`, `check_failures`, `check_duration_ms`, `open_incidents`, `worker_heartbeat`. |
| Alarms | CloudWatch Alarms → SNS email | Watches the watcher (below). |
| Email | Amazon SES, domain identity on obwebdesign.ca with DKIM | Alerts and monthly reports to Owen. SES sandbox is fine because the only recipient is Owen. |
| Secrets | SSM Parameter Store (SecureString) | DB password, API token, SES settings. Nothing secret in the repo or the image. |

### Decision record: why a single EC2 box and not Lambda, ECS or RDS
- **Lambda + RDS:** a Lambda that talks to RDS must sit inside a VPC, and a Lambda inside a VPC cannot reach the internet without a NAT gateway (about USD 30+/month on its own). A monitoring tool whose whole job is reaching the internet walks straight into that trap.
- **ECS Fargate + RDS + a load balancer:** the "big company" shape, but about USD 50–70/month before doing anything, for 11 sites.
- **One Graviton instance running compose:** roughly USD 15–20/month all-in. It also means Owen operates a real Linux server: patching, disk, memory, logs, backups, restores. That is the operations experience the postings mean.
- **The upgrade path is written down, not built:** move Postgres to RDS and the containers to ECS when there is a reason (many more sites, or a second monitoring region). Being able to explain this trade-off in an interview is worth more than the fancier diagram.

### Watching the watcher
If the worker dies, every site looks fine and nobody is told. So:
- The worker publishes `worker_heartbeat` every cycle. A CloudWatch alarm fires if it is **missing** for 15 minutes (missing data counts as breaching) and emails Owen through SNS. That path does not depend on the instance or on SES.
- Alarms also fire on disk above 80%, sustained high memory, and a failed nightly backup.

### Security
- Nothing listens publicly except Caddy on 80/443. No SSH: shell access goes through SSM Session Manager.
- Least-privilege IAM: the instance role can read its own parameters, write logs and metrics, pull from ECR, put to its backup bucket and send through SES, and nothing else. The GitHub OIDC role can push to one ECR repo and send one SSM command document.
- The dashboard and API require auth. Postgres is only reachable on the compose network.
- Outbound checks send an honest `User-Agent: Sitewatch/1.0 (+https://obwebdesign.ca)` and respect rate limits. The link crawler stays on the client's own host.
- `docs/threat-model.md`: a short threat model of the service itself (useful practice alongside SENG 360).

---

## 5. Repo layout

```
sitewatch/
  README.md                 this brief
  pyproject.toml            uv-managed; ruff, mypy, pytest config
  Dockerfile                multi-stage, non-root user, arm64 + amd64
  compose.yaml              local dev: postgres, migrate+seed, worker (api from M2)
  compose.demo.yaml         local incident demo: adds a fake site to stop and start
  compose.prod.yaml         production overrides: caddy, awslogs driver, restart policies
  Caddyfile
  alembic/                  migrations
  sites.example.yaml        site list format (the real sites.yaml is git-ignored)
  demo/                     fake site + its sites file for the local demo
  docker/                   postgres init (creates the test database)
  src/sitewatch/
    api/                    FastAPI app, routes, auth, templates/
    worker/                 scheduler, runner
    checks/                 one module per check kind, each a pure function of (config, clients) -> Result
    incidents/              incident state machine + alerting
    notify/                 SES email, templates
    db/                     models, session, repositories
    observability/          JSON logging, EMF metrics
    config.py               settings from env / SSM
    sites.py                sites.yaml validation and idempotent seeding
    cli.py                  `sitewatch migrate | seed | worker | check`
  tests/
    unit/                   checks, incident rules, report maths
    integration/            API + DB against real Postgres
  infra/                    Terraform: network/SG, EC2, IAM, ECR, S3, SES, SNS, CloudWatch, budget
  scripts/                  deploy.sh, backup.sh, restore.sh, seed_sites.py
  docs/
    architecture.md
    local-dev.md            run, demo, test
    runbook.md              deploy, roll back, restore a backup, rotate secrets, accept DNS change
    threat-model.md
    decisions/              short ADRs (compute choice, scheduling, retention)
  .github/workflows/
    ci.yml                  ruff, mypy, pytest (with a Postgres service) on every push/PR
    deploy.yml              on main: build, push to ECR, deploy via SSM, health-check
```

The real site list lives in the database (seeded from a git-ignored `sites.yaml`; `sites.example.yaml` is committed), so the repo can be public without publishing client configuration.

---

## 6. Build plan

Each milestone ends with something working and verified, not "code written". Work in order.

**M0: Accounts and tools (Owen, about 30 min)**
- Create the AWS account. Turn on MFA for the root user, then stop using root: create an IAM Identity Center admin user.
- Create a **budget alarm at USD 25/month** before anything else.
- Install: Docker (OrbStack or Docker Desktop), `uv`, `terraform`, `awscli`. Create the GitHub repo.
- Done when: `aws sts get-caller-identity` works with the admin user, and `docker run hello-world` works.

**M1: The core, locally** (code complete 2026-09-17; waiting on Docker for the final check)
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

**M3: AWS infrastructure and deploys**
- Terraform: security group, EC2 (Amazon Linux 2023, Docker installed by user-data), instance role, ECR, S3 backup bucket, SSM parameters, GitHub OIDC role.
- `deploy.yml`: build, push, SSM deploy, health-check, roll back on failure.
- Caddy serving `status.obwebdesign.ca` over HTTPS (one DNS record at the obwebdesign.ca DNS host).
- Done when: a push to `main` goes live without touching the server, and a deliberately broken build rolls back by itself.

**M4: Alerts and observability**
- SES domain identity and DKIM, alert emails (open, reminder, resolved).
- JSON logs to CloudWatch; EMF metrics; CloudWatch agent; heartbeat, disk and backup alarms; a CloudWatch dashboard.
- Checks 5–6 (DNS drift, SPF/DMARC). Baselines captured on first run.
- Nightly `pg_dump` to S3, and **one real restore tested** and written into the runbook.
- Done when: stopping the worker on the server produces the heartbeat alarm email within 15 minutes, and a real restore has been done once.

**M5: Reports and the rest of the checks**
- Daily rollups; uptime % and p50/p95 per site; 30-day views on the dashboard.
- Monthly report per site (HTML email to Owen).
- Checks 7–8 (broken links, contact form health).
- Done when: the first monthly report for all 11 sites is in Owen's inbox.

**Later, only with a reason:** public status pages per client, Slack/SMS alerts, a second probe location, RDS/ECS migration, checks for client Keystatic Cloud or Square integrations, selling it as part of a maintenance plan.

---

## 7. Turning it into résumé facts

The rules from `Career/Resume Rules.md` and `Career/Master Source.md` apply: **nothing goes on a document until it is running and verified, and every number is measured, never estimated.**

- When M3 is live, add a Sitewatch section to Master Source with the stack and what is deployed, checked against the repo and AWS.
- Numbers worth measuring once it has run for a while, each pulled from the database or CloudWatch with a date: sites monitored, checks per day, test count, real incidents caught (each described from its incident record: what broke, when, how long, what fixed it), deploys shipped through the pipeline, the monitor's own uptime.
- **Real incidents are the interview stories.** Write each one into `docs/incidents/` the week it happens, while the details are fresh.
- **Defensibility:** this will be built with Claude Code, which is fine and should be said plainly if asked. UVic's co-op AI policy draws the line at misrepresentation, so every part of the stack on the résumé has to be explainable unaided: what a Dockerfile layer is, how the OIDC deploy authenticates, why the NAT gateway trap rules out Lambda here, how an incident opens and closes, how a backup is restored. Before each milestone is called done, Owen should be able to walk through it without notes.

A draft résumé line, for once it is true:

> Built and deployed a Python monitoring service (FastAPI, Postgres, Docker, AWS) that checks 11 production client websites for downtime, certificate, domain and DNS failures, with CI/CD through GitHub Actions, Terraform, CloudWatch alarms and email alerting.

---

## 8. Cost

Roughly **USD 15–20/month**: the `t4g.small` instance, a 20 GB gp3 volume, a small amount of CloudWatch logs, metrics and alarms, S3 backups, and SES (near zero at this volume). Confirm with the AWS pricing calculator for `ca-central-1` before M3. New AWS accounts currently come with starter credits; check the terms at signup. The budget alarm in M0 is not optional.

---

## 9. Open decisions (Owen)

1. **Name.** Sitewatch is a working name.
2. **Public or private repo.** Public is better for applications (several postings ask for a GitHub profile) and is safe with the site list kept out of git. Default: public.
3. **Dashboard address.** Default: `status.obwebdesign.ca`.
