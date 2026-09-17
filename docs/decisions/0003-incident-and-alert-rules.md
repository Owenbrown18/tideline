# 0003: Incident and alert rules

**Status:** accepted, 2026-09-17 (M1)

**Decisions made while building M1, beyond the brief:**
1. **Two severities.** `warn` results open `warning` incidents, `fail` results
   open `critical` ones. TLS: under 21 days warns, under 7 days or invalid is
   critical. Domain: under 30 days warns, under 7 days, expired or on hold is
   critical. An RDAP 404 for a live site is a warning (RDAP has gaps).
2. **A fourth alert kind, `escalated`**, sent once when a warning incident
   becomes critical (a certificate going from 20 days to 6). Improving from
   critical back to warning sends nothing.
3. **Incidents are dated from the first failure** of the streak that opened
   them, not the result that crossed the threshold, so durations are honest.
4. **An alert that fails to send is retried on the next run** (the incident's
   `last_alerted_at` stays empty until one succeeds). An alert row is written
   only for alerts that actually went out.
5. **"No verdict" results are not stored** (see docs/architecture.md), so a
   site outage is one uptime incident, not three.
6. **Spam markers are phrases, not words.** "casino" alone would flag a
   musician's gig listing; "online casino" never belongs on a client site.
   A site can ignore a marker in `sites.yaml` (`spam_ignore`).
7. **`checks.key`** (for example `uptime:https://example.ca/`) identifies a check
   within its site, which makes seeding idempotent.
