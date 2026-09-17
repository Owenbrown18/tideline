#!/bin/bash
# Nightly database backup: pg_dump, gzip, upload to S3, and publish a metric so
# a backup that fails to run is noticed (the CloudWatch alarm treats missing
# data as breaching).
#
# Installed by deploy.sh as a systemd timer. Run it by hand any time:
#   /opt/sitewatch/backup.sh
set -euo pipefail

source /opt/sitewatch/env.sh
cd /opt/sitewatch

STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
FILE="/tmp/sitewatch-${STAMP}.sql.gz"
KEY="backups/sitewatch-${STAMP}.sql.gz"

compose() {
  docker compose --env-file /opt/sitewatch/compose.env -f /opt/sitewatch/compose.prod.yaml "$@"
}

metric() {
  aws cloudwatch put-metric-data --namespace Sitewatch \
    --metric-name backup_success --value "$1" --unit Count --region "$AWS_REGION" || true
}

fail() {
  echo "{\"event\":\"backup\",\"step\":\"failed\",\"reason\":\"$1\"}"
  metric 0
  rm -f "$FILE"
  exit 1
}

compose exec -T postgres pg_dump -U sitewatch --clean --if-exists sitewatch \
  | gzip -9 > "$FILE" || fail pg_dump

# A dump of a database this size is tens of KB at least; anything tiny means
# pg_dump wrote an error page instead of data.
SIZE=$(stat -c %s "$FILE")
[ "$SIZE" -gt 2000 ] || fail "dump too small (${SIZE} bytes)"

# gzip -t proves the archive is readable before it is trusted as a backup.
gzip -t "$FILE" || fail "corrupt archive"

aws s3 cp "$FILE" "s3://${SITEWATCH_BUCKET}/${KEY}" --region "$AWS_REGION" --only-show-errors \
  || fail upload

rm -f "$FILE"
metric 1
echo "{\"event\":\"backup\",\"step\":\"done\",\"key\":\"${KEY}\",\"bytes\":${SIZE}}"
