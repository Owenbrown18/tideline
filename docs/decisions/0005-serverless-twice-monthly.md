# 0005: Twice-monthly runs on Lambda, SQLite in S3

**Date:** 2026-09-18. **Supersedes:** 0001 (single EC2 instance), 0002 (in-process
scheduler), 0004 (heartbeat alarm). Those records stay as they were written.

**Context.** Version 1 checked every site every 5 minutes on an always-on
t4g.small with Postgres, for about USD 20 a month (instance 13.40, public IPv4
3.65, disks 2.65). Owen then set the real requirement: this is a portfolio
project, it must cost under USD 3 a month, and checking each site once or twice
a month is plenty. The sites are small-business brochure sites that change
rarely; if one goes down, the client or Owen notices within hours anyway. What
nobody notices is the slow failure: a domain about to lapse, a certificate
failing to renew, a contact form posting to a dead endpoint, a DMARC gap.

**Decision.**
- **Run on the 1st and 15th, at 07:00 Pacific, with EventBridge Scheduler.** The
  run on the 1st also sends the monthly reports.
- **Two Lambda functions from one container image:** the run, and the dashboard
  behind CloudFront. A full run measured 21 seconds on Lambda (86 checks).
- **SQLite in S3 instead of Postgres.** One writer, about 90 rows a run.
  Conditional uploads (If-Match) refuse to overwrite a file someone else changed;
  bucket versioning keeps 90 days of earlier copies as backups.
- **No VPC.** The functions only make outbound HTTPS requests. Outside a VPC
  they reach the internet for free; inside one they would need a NAT gateway,
  about USD 35 a month on its own.
- **One summary email per run**, sent only when something changed, instead of
  one email per incident. Incidents open on the first bad run.
- **Say what was measured.** The dashboard and reports show "up at 2 of 2
  checks" rather than uptime percentages, which two checks cannot support.

**Consequences.**
- Cost falls to cents a month: Lambda, EventBridge Scheduler, CloudFront,
  CloudWatch (within 10 metrics and alarms), SNS and SSM all stay inside AWS's
  always-free allowances. What is left is S3 and ECR storage and SES per email.
- An outage is no longer noticed within minutes, only at the next run. That is
  the trade Owen chose. For a client paying for uptime monitoring, version 1's
  design is the answer, and it is in git history.
- The heartbeat alarm (0004) no longer applies: CloudWatch alarms look back at
  most seven days, and runs are fourteen apart. A failed run raises the
  "run failed" alarm; a run that never started shows up as no report on the 1st.
- The browser's basic-auth box does not work behind a Lambda function URL
  (Lambda renames the WWW-Authenticate header, and CloudFront will not run a
  function on a 401 to rename it back, measured 2026-09-18), so the dashboard
  gained a sign-in page with a signed session cookie.
- Operating a Linux server is no longer part of the project. The version 1
  milestones (patching, disk and memory alarms, backups and a measured restore)
  happened and stay in the README as history.

**How the move was done (2026-09-18).** Alarm actions off; worker stopped; a
final `pg_dump`; the dump restored locally and counted; instance stopped;
`scripts/postgres_to_sqlite.py` copied every table into SQLite with row counts
checked (11 sites, 88 checks, 2,687 results, 28 incidents, 33 alerts, 22
rollups, 11 DNS baselines); the file uploaded to S3 and the final dump kept
under `archive/`; then one `terraform apply` removed the server and created the
Lambda setup.
