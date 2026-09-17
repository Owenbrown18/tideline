#!/bin/bash
# Deploy a Sitewatch image tag on this instance.
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
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/compose.prod.yaml" /opt/sitewatch/compose.prod.yaml --region "$AWS_REGION"
aws s3 cp "s3://$SITEWATCH_BUCKET/deploy/Caddyfile" /opt/sitewatch/Caddyfile --region "$AWS_REGION"

log write_env
# Secrets come from SSM Parameter Store at deploy time: they are never in the
# repo, the image, or Terraform state.
DB_PASSWORD=$(param /sitewatch/db_password)
umask 077
cat > /opt/sitewatch/.env <<ENVFILE
SITEWATCH_DATABASE_URL=postgresql+psycopg://sitewatch:${DB_PASSWORD}@postgres:5432/sitewatch
SITEWATCH_API_TOKEN=$(param /sitewatch/api_token)
SITEWATCH_DASHBOARD_USER=$(param /sitewatch/dashboard_user)
SITEWATCH_DASHBOARD_PASSWORD=$(param /sitewatch/dashboard_password)
SITEWATCH_LOG_LEVEL=INFO
ENVFILE

cat > /opt/sitewatch/compose.env <<COMPOSEENV
SITEWATCH_IMAGE=${SITEWATCH_ECR_REPO}:${TAG}
SITEWATCH_DB_PASSWORD=${DB_PASSWORD}
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
compose run --rm --no-deps api sitewatch migrate

log seed
# sites.yaml is deliberately not in git or the image: it is in SSM.
param /sitewatch/sites_yaml > /opt/sitewatch/sites.yaml
# The container runs as a non-root user, so the mounted file must be readable
# by it. /opt/sitewatch itself stays root-owned, and .env stays 0600.
chmod 0644 /opt/sitewatch/sites.yaml
compose run --rm --no-deps -v /opt/sitewatch/sites.yaml:/config/sites.yaml:ro api \
  sitewatch seed /config/sites.yaml

log restart
compose up -d --remove-orphans

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
    sed -i "s|^SITEWATCH_IMAGE=.*|SITEWATCH_IMAGE=${SITEWATCH_ECR_REPO}:${PREVIOUS_TAG}|" /opt/sitewatch/compose.env
    compose up -d --remove-orphans
  fi
  exit 1
fi

echo "$TAG" > "$PREVIOUS_TAG_FILE"
log done
