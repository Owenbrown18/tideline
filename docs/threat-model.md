# Threat model

A short threat model of Tideline itself (version 2: Lambda, since 2026-09-18): what it holds, who might want it, how
they could get at it, and what stops them. Written in the STRIDE style used in
SENG 360, kept deliberately small because the system is small.

## What is worth protecting

| Asset | Why it matters |
|---|---|
| **The client site list** | Which small businesses OBdesign runs, their domains, and each one's weak spots (missing SPF/DMARC, broken forms). A ready-made target list. |
| **Findings about clients** | "These 8 domains can be spoofed" is a spoofing shopping list. |
| **Alert and report email** | Tideline can send mail from `tideline@obwebdesign.ca`. Abused, it could phish, or email a client something they should never see. |
| **The AWS account** | Billing (the budget is USD 3 a month), and a trusted identity that could be used to attack others. |
| **Monitoring history** | The record that proves the sites have been healthy. Loss is embarrassing, not dangerous. |

## Who might come for it

- **Opportunistic scanners.** Anything with a public address gets probed within minutes.
- **Someone who finds the public repo** and looks for secrets or an easy way in.
- **A compromised dependency** (a Python package or base image with something added).
- **Owen, by accident.** Still the most likely cause of damage (see "Things that have actually gone wrong").

## STRIDE

### Spoofing (pretending to be someone)
| Threat | Mitigation |
|---|---|
| Someone uses the API or dashboard as Owen | Bearer token for the API; for the dashboard, a sign-in page that sets a session cookie (HttpOnly, SameSite=Lax, 30 days) signed with an HMAC key derived from the password. Both secrets are random values in SSM, compared with `secrets.compare_digest` so timing does not leak them. A wrong password costs a one-second delay. The app refuses to start if either secret is empty. |
| A forged or stolen session cookie | It holds only an expiry and a signature; changing the dashboard password invalidates every cookie at once. |
| Someone deploys as GitHub Actions | The OIDC trust policy pins both the repo (`Owenbrown18/tideline`) and the branch (`main`). A fork or another branch cannot assume the role. There are no AWS keys to steal. The role can push an image, point the two functions at it, and invoke the run (to apply migrations after a deploy). |
| Someone sends mail as `tideline@obwebdesign.ca` | Only the run function's role may call SES, and only to Owen (below). The domain has DKIM, so forged mail without the key fails authentication. |

### Tampering (changing things)
| Threat | Mitigation |
|---|---|
| Changing what runs in production | Images are pushed by tag and the ECR repository is `IMMUTABLE`, so a tag cannot be overwritten with different contents. The CI role can push images and point the two functions at one, nothing else. |
| Changing the database | It is one private S3 object; only the two function roles can write it. Uploads are conditional (If-Match), so one writer cannot silently erase another's change, and versioning keeps 90 days of earlier copies. |
| Changing the infrastructure | Terraform state is in a versioned, encrypted, private bucket; every change is a reviewable diff. |
| A malicious page tricking the checks | Pages are only read, never executed. The crawler only follows pages on the client's own host; links elsewhere and form endpoints are probed for their status alone, reading no body. Page content reaches the dashboard only through Jinja's autoescaping (there is a test for an escaped apostrophe). |
| Cross-site requests to the dashboard | The one state-changing button checks the request's Origin, and the session cookie is SameSite=Lax, so another site's form does not carry it. Sign-out is a POST. Pages cannot be framed by other sites (`frame-ancestors 'self'`, `X-Frame-Options`), so the button cannot be clickjacked. |

### Repudiation (denying something happened)
| Threat | Mitigation |
|---|---|
| "The site was never down" / "I was never told" | Every check result, incident and sent alert is a database row with a timestamp, in a versioned file. Every run's log is in CloudWatch Logs for 90 days. |

### Information disclosure (leaking things)
| Threat | Mitigation |
|---|---|
| Secrets in the public repo | None are there: `sites.yaml` is git-ignored, and all secrets live in SSM Parameter Store as SecureStrings, read by the functions when they start. They are not in the image, the function configuration or Terraform state. |
| The client list in the public repo | The real list is in SSM (`/sitewatch/sites_yaml`), not git. **Known gap:** README section 2 names the 11 domains. They are already public in OBdesign's portfolio, which is why it was accepted, but it is a choice, not an accident. |
| Client findings in the public repo | Per-domain SPF/DMARC findings live in Owen's private vault, never the repo. |
| Reading the database | A private, encrypted S3 object; public access to the bucket is blocked outright. |
| Reading traffic | CloudFront serves HTTPS only (HTTP redirects) with HSTS, and a certificate for the domain; CloudFront to Lambda is HTTPS too. The S3 bucket refuses plain HTTP. |
| Third-party code on the dashboard's domain | None. The API's generated docs pages (which loaded Swagger UI from a CDN) are switched off; the Content-Security-Policy allows scripts only from the site itself. |

### Denial of service (making it stop)
| Threat | Mitigation |
|---|---|
| Flooding the dashboard | Lambda scales, so the risk is cost, not downtime. The first 1 million Lambda requests and 10 million CloudFront requests a month are free, the account is capped at 10 concurrent executions, and the USD 3 budget alert emails Owen if a month heads over. Accepted for a one-user dashboard; AWS WAF rate limiting would be the next step, at about USD 6 a month. |
| The run failing quietly | The "run failed" alarm over SNS, which does not depend on Tideline's code. A run that never starts shows up as a missing report on the 1st. |
| Tideline being used to attack clients | Checks are polite by construction: one request per URL per run, twice a month, a pause between crawl requests, a page and link limit, and an honest user agent. |
| A hacked client page stalling or crashing the run | Links and forms are found with Python's HTML parser, whose time grows only with the page's size (the old patterns took hours on a crafted page). Probes read the status line only, so a huge file or a compression bomb costs nothing; each check has a deadline; at most 300 links per site. |
| A hacked client page aiming the checker at internal addresses (SSRF) | Every request, redirects included, is refused if its host resolves to a private, loopback or link-local address (`checks/http.py`). Lambda has no instance metadata service, so there were no credentials to steal, but its runtime API is on loopback. |

### Elevation of privilege (getting more access than intended)
| Threat | Mitigation |
|---|---|
| A hijacked GitHub Action or base image | Actions are pinned to commit SHAs and images to digests, so a moved tag upstream changes nothing; Dependabot proposes updates. The CI token can only read the repo. |
| A compromised function reaching further | One role per function. The run can read and write the one database object, read `/sitewatch/*` parameters, and send email **only to Owen** (an IAM `ses:Recipients` condition). The dashboard has the first two and no email at all. Neither can touch IAM or any other resource. |
| Tideline emailing a client | Enforced three times: the code has one recipient, IAM allows only that recipient (and refuses a request with no recipient list), and SES stays in its sandbox where unverified addresses cannot be reached at all. The contact-form check never submits a form, with a test asserting no POST is made. |
| Using the AWS account itself | Root has MFA and is not used day to day. Owen works through an IAM Identity Center user with 8-hour sessions and no long-lived keys. |
| A server to break into | There is none: no instance, no SSH, no open ports. Version 1's server was deleted on 2026-09-18. |

## Things that have actually gone wrong

- **2026-09-17: the database was destroyed by a Terraform setting** (version 1).
  `user_data_replace_on_change = true` replaced the instance when a comment
  changed. Fixed, and Postgres moved to its own protected volume.
- **2026-09-17: a broken build passed the deploy health check** (version 1).
  It checked Caddy, whose 308 redirect curl counted as success. It was changed
  to ask the app directly, and a deliberately broken image was then rejected
  and rolled back.
- **2026-09-18: renaming the alarms emailed "memory above 85%" five times.**
  Each recreated alarm reported its first "OK" state, and the subject line read
  like a problem. The one alarm left now sends only on ALARM, never on OK.
- **2026-09-18: the browser never asked for the password.** Behind a Lambda
  function URL the basic-auth header is renamed, and CloudFront does not run
  functions on a 401. Caught by probing the live URL before calling it done;
  replaced by a sign-in page.

All four were the "Owen by accident" attacker, caught by testing rather than by
a client. That is the pattern this model most wants to keep.

## The independent review, 2026-09-18

Two separate reviews (security, and correctness) were run against the code
without seeing this document's conclusions. Security found nothing critical or
high: no route without auth, no way to forge a session, no injection. It found
six medium or low-medium issues, all fixed the same day and listed above: the
crafted-page stall, unbounded probes, SSRF, missing security headers, public API
docs, and the cost of a deliberate delay on failed sign-ins. It also corrected
this document, which had claimed the link crawler never left the client's host.

## Accepted risks

- **Checks twice a month.** An outage between runs is not noticed by Tideline.
  Owen chose this (decision 0005); the clients or Owen notice a down site within
  hours anyway.
- **Probes come from one place.** A network problem between Montreal and a
  site's host looks like the site being down. The uptime check retries once
  after 30 seconds before it counts a failure.
- **One user.** The sign-in is fine for Owen alone over HTTPS. A second user
  would be the reason to add real accounts. The session key is derived from the
  40-character random password, so a stolen cookie cannot be turned back into
  it in practice; signing out deletes the browser's copy, and changing the
  password revokes every session at once.
- **The address check is made before connecting.** A domain that changes its
  DNS answer between the check and the connection (rebinding) could slip past
  it. That needs a client site's DNS under attacker control and gains only a
  status code, so it is accepted.
- **A shared concurrency limit.** New AWS accounts get 10 concurrent Lambda
  executions, shared by the run and the dashboard, and none can be reserved
  until the limit is raised. A flood could delay a run; the "run throttled"
  alarm says so, and cost stays inside the free tier or cents.
- **The GitHub account.** While it is flagged, CI does not run and deploys go
  from Owen's laptop, which holds an 8-hour SSO session rather than keys.
