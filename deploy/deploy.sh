#!/bin/bash
# Deploy a Tideline image tag on this instance.
#
#   deploy.sh <image-tag>     run that tag
#   deploy.sh                 rerun whatever tag is currently live
#
# Steps: write .env from SSM, pull the image, run migrations, restart, wait for
# /healthz. If the health check fails, roll back to the previous tag and exit
# non-zero so CI shows red.
set -euo pipefail

cd /opt/sitewatch
source /opt/sitewatch/env.sh

TAG="${1:-}"
PREVIOUS_TAG_FILE=/opt/sitewatch/current_tag
if [ -z "$TAG" ]; then
  TAG=$(cat "$PREVIOUS_TAG_FILE" 2>/dev/null || echo latest)
fi
PREVIOUS_TAG=$(cat "$PREVIOUS_TAG_FILE" 2>/dev/null || echo "")

log() { echo "{\"event\":\"deploy\",\"step\":\"$1\",\"tag\":\"$TAG\"}"; }

param() {
  aws ssm get-parameter --name "$1" --with-decryption --region "$AWS_REGION" \
    --query Parameter.Value --output text
}

log fetch_files
# Keep the running release's compose files: a rollback restores them exactly,
# because an older image may need an older compose file (the Tideline rename
# changed the command and variable names).
for f in compose.prod.yaml compose.env; do
  [ -f "/opt/sitewatch/$f" ] && cp -p "/opt/sitewatch/$f" "/opt/sitewatch/$f.previous"
done
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/compose.prod.yaml" /opt/sitewatch/compose.prod.yaml --region "$AWS_REGION"
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/Caddyfile" /opt/sitewatch/Caddyfile --region "$AWS_REGION"
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/backup.sh" /opt/sitewatch/backup.sh --region "$AWS_REGION"
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/restore.sh" /opt/sitewatch/restore.sh --region "$AWS_REGION"
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/cloudwatch-agent.json" /opt/sitewatch/cloudwatch-agent.json --region "$AWS_REGION"
chmod +x /opt/sitewatch/backup.sh /opt/sitewatch/restore.sh

log write_env
# Secrets come from SSM Parameter Store at deploy time: they are never in the
# repo, the image, or Terraform state.
DB_PASSWORD=$(param /sitewatch/db_password)
umask 077
cat > /opt/sitewatch/.env <<ENVFILE
TIDELINE_DATABASE_URL=postgresql+psycopg://sitewatch:${DB_PASSWORD}@postgres:5432/sitewatch
TIDELINE_API_TOKEN=$(param /sitewatch/api_token)
TIDELINE_DASHBOARD_USER=$(param /sitewatch/dashboard_user)
TIDELINE_DASHBOARD_PASSWORD=$(param /sitewatch/dashboard_password)
TIDELINE_LOG_LEVEL=INFO
TIDELINE_NOTIFY_CHANNEL=ses
TIDELINE_ALERT_EMAIL=${SITEWATCH_ALERT_EMAIL}
TIDELINE_ALERT_SENDER="Tideline <tideline@${SITEWATCH_DOMAIN#status.}>"
TIDELINE_PUBLIC_URL=https://${SITEWATCH_DOMAIN}
TIDELINE_AWS_REGION=${AWS_REGION}
ENVFILE
# The same settings under their pre-rename names (SITEWATCH_*), so rolling back
# to an image built before the rename still finds its configuration.
legacy=$(sed -n 's/^TIDELINE_/SITEWATCH_/p' /opt/sitewatch/.env)
printf '%s\n' "$legacy" >> /opt/sitewatch/.env

cat > /opt/sitewatch/compose.env <<COMPOSEENV
TIDELINE_IMAGE=${SITEWATCH_ECR_REPO}:${TAG}
TIDELINE_DB_PASSWORD=${DB_PASSWORD}
SITEWATCH_DOMAIN=${SITEWATCH_DOMAIN}
SITEWATCH_ALERT_EMAIL=${SITEWATCH_ALERT_EMAIL}
COMPOSEENV

compose() {
  docker compose --env-file /opt/sitewatch/compose.env -f /opt/sitewatch/compose.prod.yaml "$@"
}

log login_ecr
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${SITEWATCH_ECR_REPO%%/*}"

log pull
compose pull --quiet

log migrate
# Postgres must be up before migrations; compose starts it and waits.
compose up -d postgres
compose run --rm --no-deps api tideline migrate

log seed
# sites.yaml is deliberately not in git or the image: it is in SSM.
param /sitewatch/sites_yaml > /opt/sitewatch/sites.yaml
# The container runs as a non-root user, so the mounted file must be readable
# by it. /opt/sitewatch itself stays root-owned, and .env stays 0600.
chmod 0644 /opt/sitewatch/sites.yaml
compose run --rm --no-deps -v /opt/sitewatch/sites.yaml:/config/sites.yaml:ro api \
  tideline seed /config/sites.yaml

log restart
compose up -d --remove-orphans

log cloudwatch_agent
# Host memory and disk metrics. The agent is installed by user-data; this keeps
# its configuration in sync on every deploy.
if [ -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl ]; then
  /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s -c file:/opt/sitewatch/cloudwatch-agent.json >/dev/null
fi

log backup_timer
# Nightly pg_dump to S3 at 03:20 UTC, with a random delay so it never lines up
# exactly with anything else, and Persistent so a reboot does not skip a night.
cat > /etc/systemd/system/sitewatch-backup.service <<'UNIT'
[Unit]
Description=Tideline nightly database backup to S3
After=docker.service

[Service]
Type=oneshot
ExecStart=/opt/sitewatch/backup.sh
UNIT
cat > /etc/systemd/system/sitewatch-backup.timer <<'UNIT'
[Unit]
Description=Run the Tideline backup nightly

[Timer]
OnCalendar=*-*-* 03:20:00 UTC
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now sitewatch-backup.timer >/dev/null

log health
# Ask the api container itself, not Caddy. Going through Caddy was a trap: it
# answers port 80 with a 308 redirect to HTTPS, which curl counts as success,
# so a completely broken app passed the check. `compose exec` also fails
# outright when the container is crash-looping, which is exactly what we want.
healthy=0
for _ in $(seq 1 30); do
  state=$(docker inspect -f '{{.State.Status}}' sitewatch-api-1 2>/dev/null || echo missing)
  if [ "$state" = running ] && compose exec -T api python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=5).status==200 else 1)" \
      >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "$healthy" != 1 ]; then
  echo '{"event":"deploy","step":"unhealthy"}'
  if [ -n "$PREVIOUS_TAG" ] && [ "$PREVIOUS_TAG" != "$TAG" ]; then
    echo "{\"event\":\"deploy\",\"step\":\"rollback\",\"to\":\"$PREVIOUS_TAG\"}"
    if [ -f /opt/sitewatch/compose.env.previous ]; then
      # The previous release's own files, which already name its image tag.
      cp -p /opt/sitewatch/compose.prod.yaml.previous /opt/sitewatch/compose.prod.yaml
      cp -p /opt/sitewatch/compose.env.previous /opt/sitewatch/compose.env
    else
      sed -i "s|^TIDELINE_IMAGE=.*|TIDELINE_IMAGE=${SITEWATCH_ECR_REPO}:${PREVIOUS_TAG}|" /opt/sitewatch/compose.env
    fi
    compose up -d --remove-orphans
  fi
  exit 1
fi

echo "$TAG" > "$PREVIOUS_TAG_FILE"
log done
