# Runbook

Everything you might need to do to Sitewatch in production, with the commands.
Account 053578820490, region ca-central-1, instance `i-005da610a7d4b5079`.

Sign in first:

```bash
aws sso login --profile sitewatch
```

## Get a shell on the server
There is no SSH port. Session Manager opens a shell through the AWS API:

```bash
aws ssm start-session --target i-005da610a7d4b5079
```

Then `sudo -i`, and the stack lives in `/opt/sitewatch`:

```bash
cd /opt/sitewatch
docker compose --env-file compose.env -f compose.prod.yaml ps
docker compose --env-file compose.env -f compose.prod.yaml logs -f worker
```

## Deploy
Normally: push to `main`. GitHub Actions builds the arm64 image, pushes it to
ECR, uploads the deploy files and asks SSM to run `sitewatch-deploy`.

By hand (same steps the workflow runs):

```bash
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin 053578820490.dkr.ecr.ca-central-1.amazonaws.com
docker buildx build --platform linux/arm64 -t 053578820490.dkr.ecr.ca-central-1.amazonaws.com/sitewatch:$SHA --push .
aws s3 cp deploy/deploy.sh s3://sitewatch-data-053578820490/deploy/deploy.sh
aws s3 cp deploy/compose.prod.yaml s3://sitewatch-data-053578820490/deploy/compose.prod.yaml
aws s3 cp deploy/Caddyfile s3://sitewatch-data-053578820490/deploy/Caddyfile
aws ssm send-command --document-name sitewatch-deploy \
  --instance-ids i-005da610a7d4b5079 --parameters imageTag=$SHA
```

Watch it:

```bash
aws ssm get-command-invocation --command-id <id> --instance-id i-005da610a7d4b5079 \
  --query '[Status,StandardOutputContent]' --output text
```

## Roll back
A deploy rolls itself back: if the api container is not answering `/healthz`
after about 2 minutes, `deploy.sh` puts the previous tag back and exits
non-zero. Verified on 2026-09-17 with a deliberately broken image.

To go back on purpose, deploy an older tag:

```bash
aws ecr describe-images --repository-name sitewatch \
  --query 'reverse(sort_by(imageDetails,&imagePushedAt))[].[imageTags[0],imagePushedAt]' --output table
aws ssm send-command --document-name sitewatch-deploy \
  --instance-ids i-005da610a7d4b5079 --parameters imageTag=<older-tag>
```

The currently running tag is in `/opt/sitewatch/current_tag`.

## Rotate a secret
Secrets live in SSM Parameter Store as SecureStrings, never in git, the image or
Terraform state. Changing one and redeploying is the whole procedure:

```bash
aws ssm put-parameter --name /sitewatch/api_token --type SecureString --overwrite \
  --value "$(openssl rand -base64 32 | tr -d '/+=' | cut -c1-40)"
aws ssm send-command --document-name sitewatch-deploy \
  --instance-ids i-005da610a7d4b5079 --parameters imageTag=$(cat current_tag)
```

Read the dashboard password:

```bash
aws ssm get-parameter --name /sitewatch/dashboard_password --with-decryption \
  --query Parameter.Value --output text
```

## Run a check right now
Rather than waiting for the interval (DNS is hourly, email authentication daily):

```bash
aws ssm start-session --target i-010dc8609621b4d18
sudo -i && cd /opt/sitewatch
compose() { docker compose --env-file compose.env -f compose.prod.yaml "$@"; }
compose run --rm --no-deps api sitewatch run-once --kind dns
compose run --rm --no-deps api sitewatch run-once --kind email_auth --site davesbakery.ca
```

## Change the site list
`sites.yaml` is not in git. Production reads it from `/sitewatch/sites_yaml`:

```bash
./scripts/put_secrets.sh sites.yaml   # re-uploads the file, keeps existing passwords
aws ssm send-command --document-name sitewatch-deploy \
  --instance-ids i-005da610a7d4b5079 --parameters imageTag=$(cat /opt/sitewatch/current_tag)
```

Seeding is idempotent: sites removed from the file go inactive, and their
history is kept.

## Rebuild the server from scratch
The instance holds nothing that is not in code, S3 or SSM. The database is on a
separate volume that is detached and reattached, so history survives.

Learned the hard way on 2026-09-17: the instance used to have
`user_data_replace_on_change = true`, and editing a comment in the user-data
template destroyed the box and its database. It is now false, and the database
has its own volume.

```bash
cd infra
terraform apply -replace=aws_instance.app
```

The Elastic IP moves to the new instance, so DNS keeps working.

## Backups and restore
Nightly `pg_dump` to `s3://sitewatch-data-053578820490/backups/` at 03:20 UTC,
kept 30 days, run by the `sitewatch-backup.timer` systemd timer. The script
publishes `backup_success` to CloudWatch, and the alarm treats missing data as
breaching, so a backup that never runs is noticed within 36 hours.

The database itself lives on its own EBS volume (`sitewatch-pgdata`, 10 GB)
mounted at `/opt/sitewatch/pgdata`, so replacing the instance keeps the data.
Terraform has `prevent_destroy` on that volume.

Take one now:

```bash
aws ssm start-session --target i-005da610a7d4b5079
sudo -i && cd /opt/sitewatch
docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  pg_dump -U sitewatch sitewatch | gzip > /tmp/sitewatch-$(date +%F).sql.gz
aws s3 cp /tmp/sitewatch-$(date +%F).sql.gz s3://sitewatch-data-053578820490/backups/
```

Check a backup (restores it into a scratch database, counts the rows, drops it).
This is harmless, and the only way to know a backup is real:

```bash
aws ssm start-session --target i-010dc8609621b4d18
sudo -i
/opt/sitewatch/restore.sh                     # the newest backup
/opt/sitewatch/restore.sh --check <file>      # a named one
```

Replace the live database with a backup (stops the api and worker, rebuilds the
database from the dump, starts them again, and asks you to type `replace`):

```bash
/opt/sitewatch/restore.sh --replace latest
/opt/sitewatch/restore.sh --replace sitewatch-2026-09-17T22-58-10Z.sql.gz
```

`--replace` was tested on a local stack on 2026-09-18: every result deleted to
simulate damage, then restored from the dump, with the app restarting by itself.

Record the date, the file and the row count here each time a restore is tested.

| Date | Backup file | Restored | Result |
|---|---|---|---|
| 2026-09-17 | `sitewatch-2026-09-17T22-58-10Z.sql.gz` (11,292 bytes) | 11 sites, 66 checks, 169 results, 11 DNS baselines | Restored into `restore_test`, row counts and site names checked, database dropped. Took under a minute. |

## Accept a DNS change
DNS drift opens an incident that stays open until the new records are accepted.
Accepting stores what the latest DNS check saw as the new baseline and closes
the incident:

```bash
TOKEN=$(aws ssm get-parameter --name /sitewatch/api_token --with-decryption --query Parameter.Value --output text)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  https://status.obwebdesign.ca/sites/<id>/dns-baseline/accept
```

The old baseline is kept as history, so "what did DNS look like before" stays
answerable.

Addresses behind a CNAME are deliberately not compared: Vercel rotates the
addresses behind `www`, which produced seven false drift incidents on
2026-09-17 before the rule was added.

## Alerts and alarms
Two separate paths, on purpose:

- **Incidents** (a site is down, a certificate is expiring) go out as SES email
  from `sitewatch@obwebdesign.ca` to Owen. The app sends these.
- **Alarms** (the worker died, the disk is full, a backup failed) go through
  CloudWatch to SNS to Owen's email. This path does not run on the instance, so
  it still works when the instance does not.

Test the watch-the-watcher alarm (measured 11 min 52 s on 2026-09-18):

```bash
aws ssm start-session --target i-010dc8609621b4d18   # sudo -i, cd /opt/sitewatch
docker compose --env-file compose.env -f compose.prod.yaml stop worker
# wait, then:
aws cloudwatch describe-alarms --alarm-names sitewatch-worker-heartbeat-missing \
  --query 'MetricAlarms[].StateValue' --output text
docker compose --env-file compose.env -f compose.prod.yaml start worker
```

Silence an alarm while working on the box:

```bash
aws cloudwatch disable-alarm-actions --alarm-names sitewatch-worker-heartbeat-missing
aws cloudwatch enable-alarm-actions  --alarm-names sitewatch-worker-heartbeat-missing
```

## If the dashboard is down
1. `curl -si https://status.obwebdesign.ca/healthz` (expect 200).
2. Session Manager in, `docker ps`. Postgres unhealthy is the usual cause.
3. `docker compose ... logs --tail 100 api caddy`.
4. Disk full? `df -h`. Images pile up: `docker image prune -af`.
5. Certificate trouble? `docker compose ... logs caddy | grep -i acme`. Caddy
   needs port 80 reachable and the DNS record correct to renew.
6. Nothing else worked: `terraform apply -replace=aws_instance.app`, then restore.

## Costs
Checked against the AWS pricing API on 2026-09-17: t4g.small 13.43, 20 GB gp3
1.76, public IPv4 3.65, the rest about 1, so roughly USD 20/month. The budget
alarm at USD 25 emails Owen at 50%, 80% and when the forecast exceeds 100%.

Tear everything down (this deletes the database):

```bash
cd infra && terraform destroy
```

## Monthly reports
Built from the daily rollups, so they still work after raw results are purged.

```bash
aws ssm start-session --target i-010dc8609621b4d18
sudo -i && cd /opt/sitewatch
compose() { docker compose --env-file compose.env -f compose.prod.yaml "$@"; }

compose run --rm --no-deps api sitewatch report --month 2026-09            # print
compose run --rm --no-deps api sitewatch report --month 2026-09 --email    # email Owen
compose run --rm --no-deps api sitewatch report --month 2026-09 --site davesbakery.ca --email
```

They are also pages: `https://status.obwebdesign.ca/reports/<site_id>/2026-09`
(dashboard login).

Rollups run automatically at 00:20 UTC. To rebuild a day by hand:

```bash
compose run --rm --no-deps api sitewatch rollup --day 2026-09-17
```

Raw `check_results` older than 90 days are deleted by the same job. The rollups
are kept forever.
