#!/bin/bash
# The incident lifecycle in about a minute, on the laptop, with no AWS at all.
#
# Serves a fake client site, runs Tideline against it (fine), stops the site
# and runs again (an incident opens, and the run summary is written to the
# log instead of emailed), starts it and runs once more (it resolves).
#
#   scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

dir=$(mktemp -d)
export TIDELINE_DATABASE_URL="sqlite+aiosqlite:///$dir/demo.db"
export TIDELINE_UPTIME_RETRY_DELAY_SECONDS=1
export TIDELINE_NOTIFY_CHANNEL=log

serve() { python3 -m http.server 8765 --bind 127.0.0.1 --directory demo/fakesite >/dev/null 2>&1 & echo $!; }
say() { printf '\n== %s\n' "$1"; }
summary() { grep -E '"event": "(run|alert|report)"' || true; }

uv run tideline migrate >/dev/null 2>&1
site=$(serve); sleep 1
trap 'kill $site 2>/dev/null || true' EXIT

say "1. The site is up"
uv run tideline run --sites demo/sites.demo.yaml 2>&1 | summary

say "2. The site goes down: an incident opens, and the run's summary is sent (logged here)"
kill "$site"; sleep 1
uv run tideline run 2>&1 | summary

say "3. The site comes back: the incident resolves"
site=$(serve); sleep 1
uv run tideline run 2>&1 | summary

say "Open incidents now"
sqlite3 "$dir/demo.db" "select id, opened_at, resolved_at, severity, summary from incidents;"
