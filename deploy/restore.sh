#!/bin/bash
# Restore a Sitewatch database backup.
#
#   restore.sh                          check the newest backup: restore it into a
#                                       scratch database, count the rows, drop it
#   restore.sh --check  <key>           the same, for a named backup
#   restore.sh --replace <key|latest>   replace the LIVE database with a backup
#   restore.sh --replace --file <path>  ... from a local file instead of S3
#
# --check is harmless and should be run regularly: a backup that has never been
# restored is not a backup. --replace stops the api and worker, rebuilds the
# database from the dump, and starts them again. It asks for confirmation unless
# --yes is given.
#
# Runs on the instance by default. Set COMPOSE to run it anywhere else, e.g.
#   COMPOSE="docker compose" SITEWATCH_BUCKET=unused ./deploy/restore.sh --replace --file x.sql.gz --yes
set -euo pipefail

if [ -f /opt/sitewatch/env.sh ]; then
  source /opt/sitewatch/env.sh
  cd /opt/sitewatch
fi
COMPOSE=${COMPOSE:-"docker compose --env-file /opt/sitewatch/compose.env -f /opt/sitewatch/compose.prod.yaml"}
AWS_REGION=${AWS_REGION:-ca-central-1}

mode=check
key=latest
file=""
yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode=check; shift; [ $# -gt 0 ] && [[ "$1" != --* ]] && { key=$1; shift; } ;;
    --replace) mode=replace; shift; [ $# -gt 0 ] && [[ "$1" != --* ]] && { key=$1; shift; } ;;
    --file) file=$2; shift 2 ;;
    --yes) yes=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { echo "{\"event\":\"restore\",\"mode\":\"$mode\",\"step\":\"$1\"${2:+,$2}}"; }
psql() { $COMPOSE exec -T postgres psql -U sitewatch -v ON_ERROR_STOP=1 "$@"; }

# 1. Get the dump.
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
dump="$workdir/restore.sql.gz"
if [ -n "$file" ]; then
  cp "$file" "$dump"
  source_name=$file
else
  if [ "$key" = latest ]; then
    key=$(aws s3 ls "s3://${SITEWATCH_BUCKET}/backups/" --region "$AWS_REGION" | sort | tail -1 | awk '{print $4}')
    [ -n "$key" ] || { echo "no backups found in s3://${SITEWATCH_BUCKET}/backups/" >&2; exit 1; }
  fi
  aws s3 cp "s3://${SITEWATCH_BUCKET}/backups/${key#backups/}" "$dump" --region "$AWS_REGION" --only-show-errors
  source_name=$key
fi
gzip -t "$dump" || { echo "the backup archive is corrupt" >&2; exit 1; }
log fetched "\"source\":\"$source_name\",\"bytes\":$(wc -c < "$dump" | tr -d ' ')"

counts() {
  psql -d "$1" -tA -c "select 'sites=' || (select count(*) from sites) || ' checks=' || (select count(*) from checks) || ' results=' || (select count(*) from check_results) || ' incidents=' || (select count(*) from incidents) || ' rollups=' || (select count(*) from daily_rollups);"
}

if [ "$mode" = check ]; then
  psql -q -c "DROP DATABASE IF EXISTS restore_check;" -c "CREATE DATABASE restore_check;"
  gunzip -c "$dump" | psql -q -d restore_check >/dev/null
  result=$(counts restore_check)
  psql -q -c "DROP DATABASE restore_check;"
  log verified "\"counts\":\"$result\""
  exit 0
fi

# --replace from here on.
if [ "$yes" != 1 ]; then
  echo "This REPLACES the live Sitewatch database with $source_name."
  echo "Current: $(counts sitewatch)"
  read -r -p "Type 'replace' to continue: " answer
  [ "$answer" = replace ] || { echo "cancelled"; exit 1; }
fi

log before "\"counts\":\"$(counts sitewatch)\""
# Stop everything that writes, so nothing lands between the drop and the restore.
$COMPOSE stop worker api >/dev/null
psql -q -d postgres -c "DROP DATABASE sitewatch WITH (FORCE);" -c "CREATE DATABASE sitewatch OWNER sitewatch;"
gunzip -c "$dump" | psql -q -d sitewatch >/dev/null
$COMPOSE start api worker >/dev/null
log after "\"counts\":\"$(counts sitewatch)\""
