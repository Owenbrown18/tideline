# Runbook

Everything you might need to do to Tideline in production, with the commands.
Account 053578820490, region ca-central-1. Two Lambda functions:
`tideline-run` (the checks, 1st and 15th at 07:00 Pacific) and `tideline-web`
(the dashboard, behind CloudFront).

Sign in first:

```bash
aws sso login --profile sitewatch
export AWS_PROFILE=sitewatch AWS_REGION=ca-central-1
```

## Sign in to the dashboard
https://status.obwebdesign.ca/ (username `owen`). The password is in Parameter
Store; this prints it in your own terminal only:

```bash
aws ssm get-parameter --name /sitewatch/dashboard_password --with-decryption \
  --query Parameter.Value --output text
```

## See what happened
Every check result is one JSON line in the run's log:

```bash
aws logs tail /aws/lambda/tideline-run --since 2d
aws logs tail /aws/lambda/tideline-run --since 2d --filter-pattern '"run_finished"'
aws logs tail /aws/lambda/tideline-web --since 1h      # the dashboard
```

## Run the checks now
Rather than waiting for the 1st or 15th (it takes about 20 seconds):

```bash
aws lambda invoke --function-name tideline-run --cli-binary-format raw-in-base64-out \
  --payload '{"task":"run"}' /dev/stdout
# only one kind, or one site, e.g. after fixing something:
aws lambda invoke --function-name tideline-run --cli-binary-format raw-in-base64-out \
  --payload '{"task":"run","kind":"email_auth","site":"davesbakery.ca"}' /dev/stdout
```

A run emails Owen only if something changed since the last one.

## Deploy
From the laptop, with a clean git tree:

```bash
scripts/deploy.sh
```

It builds the arm64 image, pushes it to ECR tagged with the commit, points both
functions at it, applies any new migration, and checks `/healthz`. The GitHub
Actions workflow (`.github/workflows/deploy.yml`) does the same on every push to
`main`, once Actions is enabled on the account.

## Roll back
Point both functions at an earlier image (ECR keeps the last 3):

```bash
aws ecr describe-images --repository-name sitewatch \
  --query 'reverse(sort_by(imageDetails,&imagePushedAt))[].[imageTags[0],imagePushedAt]' --output text
for fn in tideline-run tideline-web; do
  aws lambda update-function-code --function-name $fn \
    --image-uri 053578820490.dkr.ecr.ca-central-1.amazonaws.com/sitewatch:<tag>
done
```

A migration is not undone by this; so far every migration only adds things, so
older code keeps working against a newer database.

## Change the site list
`sites.yaml` is not in git. Production reads it from Parameter Store at the
start of every run:

```bash
aws ssm put-parameter --name /sitewatch/sites_yaml --type SecureString --overwrite \
  --value "$(cat sites.yaml)"
```

It takes effect at the next run; run now (above) to apply it straight away.

## Rotate a secret

```bash
aws ssm put-parameter --name /sitewatch/dashboard_password --type SecureString \
  --overwrite --value "$(openssl rand -base64 30)"
# make both functions start fresh, so they read it now rather than within the hour:
for fn in tideline-run tideline-web; do
  aws lambda update-function-configuration --function-name $fn \
    --description "secrets rotated $(date +%F)" >/dev/null
done
```

Changing the dashboard password also signs every browser out (the session
cookie is signed with a key derived from it).

## Backups and restore
The database is one file, `s3://sitewatch-data-053578820490/tideline.db`, and
the bucket keeps every earlier version for 90 days: each run's upload is a
backup of the state before it. List them:

```bash
aws s3api list-object-versions --bucket sitewatch-data-053578820490 --prefix tideline.db \
  --query 'Versions[].[VersionId,LastModified,Size]' --output text
```

Check a version (download it and count the rows, touching nothing live):

```bash
aws s3api get-object --bucket sitewatch-data-053578820490 --key tideline.db \
  --version-id <id> /tmp/check.db >/dev/null
sqlite3 /tmp/check.db "select count(*) from sites; select count(*) from check_results;"
```

Restore it (copying an old version over the current one makes it the newest):

```bash
aws s3api copy-object --bucket sitewatch-data-053578820490 --key tideline.db \
  --copy-source "sitewatch-data-053578820490/tideline.db?versionId=<id>"
```

The dashboard picks it up on its next request; the next run starts from it.

The final Postgres backup from version 1 is kept, not expiring, at
`archive/final-postgres-2026-09-18T05-25-51Z.sql.gz`.
`scripts/postgres_to_sqlite.py` turns such a dump (restored into a local
Postgres) into a SQLite file.

| Date | What was restored | Result |
|---|---|---|
| 2026-09-17 | Postgres dump `sitewatch-2026-09-17T22-58-10Z.sql.gz` into a scratch database | 11 sites, 66 checks, 169 results, 11 DNS baselines. Under a minute. |
| 2026-09-18 | Final Postgres dump, restored locally, then copied to SQLite | Every table's row count matched: 11 sites, 88 checks, 2,687 results, 28 incidents, 33 alerts, 22 rollups, 11 baselines. |

## Accept a DNS change
DNS drift opens an incident that stays open until the new records are accepted.
On the dashboard: open the site, and press "Accept the new DNS records". Or:

```bash
TOKEN=$(aws ssm get-parameter --name /sitewatch/api_token --with-decryption --query Parameter.Value --output text)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  https://status.obwebdesign.ca/sites/<id>/dns-baseline/accept
```

The old baseline is kept as history. Addresses behind a CNAME are deliberately
not compared: Vercel rotates the addresses behind `www`, which produced seven
false drift incidents on 2026-09-17 before the rule was added.

## Emails and alarms
Two separate paths, on purpose:

- **Run summaries and monthly reports** go out as SES email from
  `tideline@obwebdesign.ca` to Owen. The run sends these. A summary goes only
  when something changed; reports go with the run on the 1st.
- **The "run failed" alarm** (`tideline-run-failed`) goes through CloudWatch to
  SNS to Owen's email. It does not depend on Tideline's code working.

Resend a month's reports:

```bash
aws lambda invoke --function-name tideline-run --cli-binary-format raw-in-base64-out \
  --payload '{"task":"report","month":"2026-09"}' /dev/stdout
```

Reports are also pages: `https://status.obwebdesign.ca/reports/<site_id>/2026-09`.

## The dashboard's address
`status.obwebdesign.ca` is a CNAME at Hostinger to CloudFront. CloudFront's own
address and the records it needs are Terraform outputs:

```bash
cd infra && terraform output dns_record_for_the_dashboard certificate_validation_record certificate_status
```

The certificate (ACM, in us-east-1) is validated by a CNAME at Hostinger and
renews itself as long as that record stays.

## The public demo
https://tideline.obwebdesign.ca is `tideline-demo`: the dashboard in demo mode
(no sign-in, read-only) over `s3://sitewatch-data-053578820490/showcase.db`.
The scheduler rebuilds that file at 06:00 Pacific every day; rebuild it now:

```bash
aws lambda invoke --function-name tideline-demo --cli-binary-format raw-in-base64-out \
  --payload '{"task":"showcase"}' /dev/stdout
```

It has its own role (the showcase file only) and cannot see real data. To take
product photos from the same data: `scripts/photos.sh` (Chrome and ffmpeg).

## If the dashboard is down
1. `curl -si https://status.obwebdesign.ca/healthz` (expect 200).
2. `aws logs tail /aws/lambda/tideline-web --since 30m` for the error.
3. The function URL directly (skips CloudFront): `terraform output function_url`,
   then `curl -si <url>healthz`. If that works, the problem is CloudFront or DNS.
4. `aws lambda get-function --function-name tideline-web --query 'Configuration.[State,LastUpdateStatus]'`.

## Costs
Expected under USD 1 a month (README section 8). A budget (`tideline-monthly`)
emails Owen when the month's actual or forecast bill passes USD 3. Check the
month so far:

```bash
aws ce get-cost-and-usage --time-period Start=$(date +%Y-%m-01),End=$(date +%F) \
  --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE
```

Tear everything down (functions, schedule, CloudFront, alarm). Terraform will
refuse to delete the S3 bucket while it still holds the database, which is the
point: empty it by hand only if the history really is not wanted.

```bash
cd infra && terraform destroy -var image_tag=unused
```
