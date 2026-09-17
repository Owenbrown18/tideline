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
The instance holds nothing that is not in code, S3 or SSM, except the Postgres
volume. Restore a backup afterwards (below).

```bash
cd infra
terraform apply -replace=aws_instance.app
```

The Elastic IP moves to the new instance, so DNS keeps working.

## Backups and restore
Nightly `pg_dump` to `s3://sitewatch-data-053578820490/backups/`, kept 30 days
(M4 adds the schedule and the failure alarm).

Take one now:

```bash
aws ssm start-session --target i-005da610a7d4b5079
sudo -i && cd /opt/sitewatch
docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  pg_dump -U sitewatch sitewatch | gzip > /tmp/sitewatch-$(date +%F).sql.gz
aws s3 cp /tmp/sitewatch-$(date +%F).sql.gz s3://sitewatch-data-053578820490/backups/
```

Restore into a scratch database and check it, which is the only way to know a
backup is real:

```bash
aws s3 cp s3://sitewatch-data-053578820490/backups/<file>.sql.gz /tmp/
docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  psql -U sitewatch -c "CREATE DATABASE restore_test;"
gunzip -c /tmp/<file>.sql.gz | docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  psql -U sitewatch -d restore_test
docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  psql -U sitewatch -d restore_test -c "select count(*) from check_results;"
docker compose --env-file compose.env -f compose.prod.yaml exec -T postgres \
  psql -U sitewatch -c "DROP DATABASE restore_test;"
```

Record the date, the file and the row count here each time a restore is tested.
Restores tested so far: none yet (due in M4).

## Accept a DNS change (M4)
DNS drift opens an incident that stays open until the new records are accepted:

```bash
curl -X POST -H "Authorization: Bearer $SITEWATCH_API_TOKEN" \
  https://status.obwebdesign.ca/sites/<id>/dns-baseline/accept
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
