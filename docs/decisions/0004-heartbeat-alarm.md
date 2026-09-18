# 0004: The heartbeat alarm fills gaps with zero

> **Superseded on 2026-09-18 by [0005](0005-serverless-twice-monthly.md)** (twice-monthly runs on Lambda). Kept as it was decided.

**Status:** accepted, 2026-09-18 (after three measured attempts)

**Context.** If the worker dies, every site looks healthy and nobody is told.
The worker publishes `worker_heartbeat` = 1 every minute; the alarm has to fire
when that stops. The brief asked for detection within 15 minutes.

**What was tried, each measured by stopping the worker on the live instance:**

| Attempt | Configuration | Time to alarm |
|---|---|---|
| 1 | 3 x 5-minute periods, missing data treated as breaching | 25 minutes |
| 2 | 10 x 1-minute periods, missing data treated as breaching | no alarm after 23 minutes |
| 3 | 10 x 1-minute periods over `FILL(heartbeats, 0)` | **11 minutes 52 seconds** |

**Why the first two were slow.** CloudWatch does not treat a missing datapoint
as missing straight away: it waits past the evaluation window for late data
first, and that wait grows as periods get shorter. An alarm built on "missing
data counts as breaching" therefore fires late, and later still with short
periods, which is the opposite of the intuition behind attempt 2.

**Decision.** A metric-math alarm on `FILL(heartbeats, 0)`. Every minute with
no heartbeat becomes an explicit 0, so the alarm evaluates real datapoints and
fires on the tenth consecutive zero. `treat_missing_data = breaching` is kept
as a backstop for the case where the query returns nothing at all.

**Consequences.** Detection about 12 minutes, inside the 15 the brief asked
for. The notification path is CloudWatch to SNS to email, which does not run
on the instance, so it works when the instance does not.
