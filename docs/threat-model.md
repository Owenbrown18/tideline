# Threat model

A short threat model of Tideline itself: what it holds, who might want it, how
they could get at it, and what stops them. Written in the STRIDE style used in
SENG 360, kept deliberately small because the system is small.

## What is worth protecting

| Asset | Why it matters |
|---|---|
| **The client site list** | Which small businesses OBdesign runs, their domains, and each one's weak spots (missing SPF/DMARC, broken forms). A ready-made target list. |
| **Findings about clients** | "These 8 domains can be spoofed" is a spoofing shopping list. |
| **Alert and report email** | Tideline can send mail from `tideline@obwebdesign.ca`. Abused, it could phish, or email a client something they should never see. |
| **The AWS account** | Billing, and a trusted identity that could be used to attack others. |
| **Monitoring history** | The record that proves the sites have been healthy. Loss is embarrassing, not dangerous. |

## Who might come for it

- **Opportunistic scanners.** Anything on a public IP gets probed within minutes.
- **Someone who finds the public repo** and looks for secrets or an easy way in.
- **A compromised dependency** (a Python package or base image with something added).
- **Owen, by accident.** Still the most likely cause of damage, as 2026-09-17 showed (below).

## STRIDE

### Spoofing (pretending to be someone)
| Threat | Mitigation |
|---|---|
| Someone uses the API or dashboard as Owen | Bearer token for the API, basic auth for the dashboard, both random 40-character values in SSM. Compared with `secrets.compare_digest` so timing does not leak them. The app refuses to start if either is empty. |
| Someone deploys as GitHub Actions | The OIDC trust policy pins both the repo (`Owenbrown18/sitewatch`) and the branch (`main`). A fork or another branch cannot assume the role. There are no AWS keys to steal. |
| Someone sends mail as `tideline@obwebdesign.ca` | Only the instance role may call SES, and only to Owen (next section). The domain has DKIM, so forged mail without the key fails authentication. |

### Tampering (changing things)
| Threat | Mitigation |
|---|---|
| Changing what runs in production | Images are pushed by tag and the ECR repo is `IMMUTABLE`, so a tag cannot be overwritten with different contents. |
| Running arbitrary commands on the server through CI | CI may run exactly one SSM document (`sitewatch-deploy`) on exactly one instance. It cannot run anything else. The document validates the tag against `^[A-Za-z0-9._-]{1,128}$`, so a tag cannot smuggle in shell. |
| Changing the infrastructure | Terraform state is in a versioned, encrypted, private bucket; every change is a reviewable diff. |
| A malicious page tricking the checks | Pages are only read, never executed. The link crawler stays on the client's own host. Page content reaches the dashboard only through Jinja's autoescaping (there is a test for an escaped apostrophe). |

### Repudiation (denying something happened)
| Threat | Mitigation |
|---|---|
| "The site was never down" / "I was never told" | Every check result, incident and sent alert is a database row with a timestamp, backed up nightly. Every container's output is in CloudWatch Logs for 30 days. |

### Information disclosure (leaking things)
| Threat | Mitigation |
|---|---|
| Secrets in the public repo | None are there: `sites.yaml` is git-ignored, and all secrets live in SSM Parameter Store as SecureStrings, read at deploy time. Terraform never sees their values, so they are not in state either. |
| The client list in the public repo | The real list is in SSM (`/sitewatch/sites_yaml`), not git. **Known gap:** README section 2 names the 11 domains. They are already public in OBdesign's portfolio, which is why it was accepted, but it is a choice, not an accident. |
| Client findings in the public repo | Per-domain SPF/DMARC findings live in Owen's private vault, never the repo. |
| Reading the database | Postgres has no published port: it is reachable only on the compose network. The EBS volumes and S3 buckets are encrypted and private. |
| Reading traffic | Caddy serves HTTPS only, with HSTS; HTTP only redirects. |

### Denial of service (making it stop)
| Threat | Mitigation |
|---|---|
| Flooding the dashboard | It is one small box, and this is accepted: the dashboard is for Owen, and the worker (the part that matters) keeps checking even if the API is overwhelmed. Caddy and the API run in separate containers from the worker. |
| The worker dying quietly | The heartbeat alarm, over SNS, which does not depend on the instance at all. |
| Filling the disk | Log retention is 30 days in CloudWatch; raw results are purged after 90 days; ECR keeps 10 images; a disk alarm fires at 80%. |
| Tideline being used to attack clients | Checks are polite by construction: one request per URL per interval, a pause between crawl requests, a page limit, and an honest user agent. |

### Elevation of privilege (getting more access than intended)
| Threat | Mitigation |
|---|---|
| A container compromise becoming a host compromise | Containers run as a non-root user (uid 10001). |
| A host compromise becoming an account compromise | The instance role can read only `/sitewatch/*` parameters, pull only its own image, write only its own bucket and log group, publish only the `Tideline` metric namespace, and send email **only to Owen** (an IAM `ses:Recipients` condition). It cannot touch IAM, EC2, or any other resource. |
| Tideline emailing a client | Enforced three times: the code has one recipient, IAM allows only that recipient, and SES stays in its sandbox where unverified addresses cannot be reached at all. The contact-form check never submits a form, with a test asserting no POST is made. |
| Using the AWS account itself | Root has MFA and is not used day to day. Owen works through an IAM Identity Center user with 8-hour sessions and no long-lived keys. A USD 25 budget alarm catches abuse that costs money. |
| No SSH to break into | The security group has no port 22. Shell access is SSM Session Manager, authenticated through AWS. |

## Things that have actually gone wrong

- **2026-09-17: the database was destroyed by a Terraform setting.**
  `user_data_replace_on_change = true` replaced the instance when a comment
  changed. Now false, and Postgres has its own volume with `prevent_destroy`.
- **2026-09-17: a broken build passed the deploy health check.** It checked
  Caddy, whose 308 redirect curl counted as success. It now asks the app
  container directly, and a deliberately broken image was rejected and rolled
  back.

Both were the "Owen by accident" attacker, caught by testing rather than by an
outage. That is the pattern this model most wants to keep.

## Accepted risks

- **One instance, one availability zone.** An AZ outage takes Tideline down,
  and nothing tells Owen except the heartbeat alarm. Acceptable for 11 sites;
  the upgrade path is in `docs/decisions/0001-single-ec2-instance.md`.
- **Probes come from one place.** A network problem between Montreal and a
  site's host looks like the site being down. Uptime needs two failures in a row
  plus a retry before it opens an incident, which absorbs most of this.
- **Basic auth on the dashboard.** Fine for one user over HTTPS. A second user
  would be the reason to add real accounts.
- **The GitHub account.** While it is flagged, CI does not run and deploys go
  from Owen's laptop, which holds an 8-hour SSO session rather than keys.
