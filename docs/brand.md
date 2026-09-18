# Tideline brand

Tideline is OBdesign's website monitoring service. It is endorsed "by OBdesign"
and has its own blue identity so it reads as a tool, not as the studio. Its
working name was Sitewatch (see "The rename" below).

The full proposal, with every board, is the brand canvas:
https://claude.ai/artifact/V8A2dKs2WJL8Su5XogUFNJ

The code version of this page is `src/tideline/brand.py` (words, status mapping,
favicon) and `src/tideline/api/static/tideline.css` (colours, type, layout).

## The idea

A tideline is the mark the water leaves: the quiet record of where things were.
The mark is **T.** and the period is the status light. On the dashboard, the
favicon and the alert emails, the period takes the colour and shape of the worst
current status.

Tagline: *Quiet until it matters.*

## Colour

| Token | Hex | Use |
|---|---|---|
| Deep | `#0b1826` | Header, buttons, favicon ground |
| Deep panel | `#122338` | Panels on Deep |
| Gradient | `#2c4a6e` → `#1b3452` → `#0b1826` | Hero and report header (radial, top left) |
| Tide | `#86b0d6` | The period when all is well, on Deep |
| Paper | `#f7f6f3` | Page background, text on Deep |
| Ink | `#1e1e1e` | Body text |
| Slate | `#636c68` | Secondary text, labels |
| Mist | `#a9b8c6` | Secondary text on Deep |

OBdesign keeps its own forest `#0b1f1d` and sage `#7ba49e`, used only for the
OBdesign wordmark in report footers.

## Status

Status is never colour alone. Every status has a **shape**, a **word** and a
colour, so it reads in greyscale, for colour-blind readers and to screen readers.

| Tone | Word | Shape | Fill | Text | On Deep |
|---|---|---|---|---|---|
| up | Up | circle | `#2f6fa6` | `#255d8c` | `#86b0d6` |
| warn | Warning | triangle | `#a8740f` | `#8a5a0f` | `#e0a93f` |
| down | Down | square | `#c0432f` | `#a8321f` | `#e8705a` |
| none | No data | hollow ring | `#848d89` | `#636c68` | `#6f7a86` |

Check results map to tones: `ok` is up, `warn` is warn, `fail` is down, and no
result yet is none. A group (a site, the whole dashboard) takes its worst tone.

## Type

- **Fraunces**: the voice. Headlines, big numbers, the wordmark.
- **Inter**: the interface. Everything else.
- **IBM Plex Mono**: evidence. Domains, URLs, response times, record values.

Emails fall back to Georgia and the system sans, because most mail clients do not
load web fonts.

## Voice

Calm and specific. One sentence that says how things are, then the facts.

- Say how things are first: "All 11 sites are up." "Two sites need you."
- Name the site, say what is wrong, say what to do.
- Plain words over check names: "The contact form isn't delivering", not "form check failed".
- No exclamation marks, no em dashes, no alarm words.
- Numbers under thirteen are words in headlines ("Two sites need you"); figures elsewhere.
- Dates the way a person writes them: 14 December 2026.
- Times in Pacific time (the display zone), labelled; everything stored stays UTC.
- Say what was measured: "up at 2 of 2 checks", not an uptime percentage two checks cannot support.

### Alert subjects

| Alert | Subject |
|---|---|
| Opened, critical | `Down: SOMA Active Health, contact form` |
| Opened, warning | `Warning: Figs & Honey, email authentication` |
| Escalated | `Now critical: …` |
| Daily reminder | `Still down: …` / `Still a warning: …` |
| Resolved | `Fixed: Figs & Honey, after 47 min` |

Uptime alerts leave the check off (`Down: SOMA Active Health`), because the site
being down is the whole message.

### Client words

The monthly report is written for a client to read when Owen forwards it. It
uses `CLIENT_WORDS` in `reports/monthly.py` ("The contact form can deliver"),
never check names, and only critical incidents appear under "What happened".
Standing gaps like a missing DMARC record show in "What was watched" instead.

## Layout

- Paper page, Deep header with the gradient hero.
- The strip of recent checks: one cell per run (the last 12, six months), up,
  down, or up only on the retry. Hollow cells are runs before Tideline was
  watching.
- Works at phone width: the nav becomes a tab row under the header below 860px,
  and less important table columns hide below 860px and 560px.
- Motion only where it carries meaning (the status period breathes), and none
  when the reader asks for reduced motion.

## The rename

Sitewatch was the working name until 2026-09-17, when it turned out to collide
with getsitewatch.com, a monitoring product for agencies. Everything a person
sees, and everything in the code, now says Tideline: the dashboard, alert
emails, reports, the Python package (`tideline`), the CLI (`tideline run`),
settings (`TIDELINE_*`), log names, the user agent sent to client sites, and
the docs.

Some names still say `sitewatch`, on purpose: they belong to AWS resources that
hold data, secrets or permissions, and AWS cannot rename them in place.
Renaming would mean replacing each one (moving the database and its history,
moving the secrets, and confirming the alarm email again) for a name nobody sees.

| Name | What it is |
|---|---|
| `sitewatch-data-053578820490` | the S3 bucket holding the database |
| `sitewatch-tfstate-053578820490` | the S3 bucket holding Terraform's state |
| `sitewatch` | the ECR image repository |
| `/sitewatch/*` | the SSM parameters: secrets and the site list |
| `sitewatch-alarms` | the SNS topic that emails alarms to Owen |
| `sitewatch-github-deploy` | the IAM role GitHub Actions assumes |
| `--profile sitewatch` | the AWS CLI profile on Owen's laptop |
| `~/OBDesign/Systems/sitewatch` | the local folder |

Version 1's server names (`/opt/sitewatch`, the `sitewatch` Postgres database,
the compose project) went away with the server on 2026-09-18.

Everything created since is named `tideline`: the two Lambda functions and
their roles, the schedule, the alarm, the CloudWatch dashboard, the budget, the
metric namespace (`Tideline`), and every resource's tags. The GitHub deploy trust
names `repo:Owenbrown18/tideline`.
