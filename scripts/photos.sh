#!/bin/bash
# Product photos of Tideline, from the showcase data (invented businesses only).
#
#   scripts/photos.sh ["$HOME/OBDesign/Content/Posts/OBdesign/2026-09 Tideline Screens"]
#
# Builds a fresh showcase database, serves it on this laptop in demo mode with
# the "invented businesses" banner hidden, and takes every screenshot with
# headless Chrome: desktop at 2x, phones at 3x, the run-summary email, and a
# monthly report. Needs Google Chrome and ffmpeg (for cropping phone shots).
set -euo pipefail
cd "$(dirname "$0")/.."

out=${1:-"$HOME/OBDesign/Content/Posts/OBdesign/$(date +%Y-%m) Tideline Screens"}
mkdir -p "$out"
work=$(mktemp -d)
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
port=8765
frame=src/tideline/api/static/_phone-frame.html

echo "building the showcase (about a minute: it runs six months of checks)"
uv run tideline showcase --out "$work/showcase.db" >/dev/null
TIDELINE_API_PORT=$port TIDELINE_DEMO_MODE=true TIDELINE_DEMO_BANNER=false \
  TIDELINE_DATABASE_URL="sqlite+aiosqlite:///$work/showcase.db" uv run tideline api >/dev/null 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true; rm -f "$frame"' EXIT
sleep 4
base="http://127.0.0.1:$port"

shot() {  # name width height scale url
  "$chrome" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor="$4" \
    --window-size="$2,$3" --virtual-time-budget=6000 --screenshot="$out/$1" "$5" >/dev/null 2>&1
}
# Headless Chrome will not lay out narrower than 500 px, so a phone is shot
# through a 390 px frame served from the same origin (the dashboard only allows
# itself to frame its pages), then cropped to the frame.
phone() {  # name path
  printf '<!doctype html><style>html,body{margin:0}iframe{border:0;width:390px;height:844px;display:block}</style><iframe src="%s"></iframe>' "$2" > "$frame"
  "$chrome" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=3 \
    --window-size=600,844 --virtual-time-budget=8000 --screenshot="$work/raw.png" \
    "$base/static/_phone-frame.html" >/dev/null 2>&1
  ffmpeg -loglevel error -y -i "$work/raw.png" -vf "crop=1170:2532:0:0" "$out/$1"
}

shot 01-overview.png 1440 1366 2 "$base/"
shot 02-site-page.png 1440 1187 2 "$base/sites/4/view"
shot 03-incidents.png 1440 900 2 "$base/incidents/view"
shot 04-report-dialog.png 1440 1000 2 "$base/reports#report=1/2026-06"
phone 05-phone-overview.png "/"
phone 06-phone-site.png "/sites/6/view"
shot 08-monthly-report.png 680 812 2 "$base/reports/1/2026-06"
phone 09-phone-report.png "/reports/1/2026-06"

# The run-summary email, rendered by the real template with the showcase's news.
TIDELINE_PUBLIC_URL=https://tideline.obwebdesign.ca uv run python - "$work/digest.html" <<'PY'
import sys
from datetime import UTC, datetime, timedelta
from tideline.notify.base import AlertMessage
from tideline.notify.digest import RunSummary, StillOpen, render_html
at = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
news = [
    AlertMessage("open", "Ridgeback Roofing", "ridgebackroofing.ca", "uptime", "u", "critical",
                 "The site is down (HTTP 503)", at, site_id=4),
    AlertMessage("open", "Northwind Dental", "northwinddental.ca", "form", "f", "critical",
                 "The form posts to /api/contact, which returns 404", at, site_id=6),
]
still = [StillOpen("Saltspring Pottery Studio", "email_auth", "warning", "No DMARC record",
                   at - timedelta(days=167))]
open(sys.argv[1], "w").write(render_html(RunSummary.of(news, still, at)))
PY
shot 07-run-summary-email.png 680 752 2 "file://$work/digest.html"

echo "photos in: $out"
