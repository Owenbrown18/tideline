#!/bin/bash
# Deploy from the laptop: build the image, push it, point both functions at it.
# The same steps .github/workflows/deploy.yml runs.
#
#   aws sso login --profile sitewatch
#   scripts/deploy.sh
set -euo pipefail

export AWS_PROFILE=${AWS_PROFILE:-sitewatch}
export AWS_REGION=${AWS_REGION:-ca-central-1}
REGISTRY=053578820490.dkr.ecr.${AWS_REGION}.amazonaws.com
REPO=${REGISTRY}/sitewatch
TAG=$(git rev-parse --short HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo "uncommitted changes: commit first, so the tag names exactly this code" >&2
  exit 1
fi

log() { echo "{\"event\":\"deploy\",\"step\":\"$1\",\"tag\":\"$TAG\"}"; }

log build
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
# --provenance=false: Lambda accepts a plain image, not a multi-part image index.
docker buildx build --quiet --platform linux/arm64 --provenance=false -t "$REPO:$TAG" --push . >/dev/null

for fn in tideline-run tideline-web; do
  log "update_$fn"
  aws lambda update-function-code --function-name "$fn" --image-uri "$REPO:$TAG" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
done

# New code may bring a new migration: apply it now, not at the next run, so the
# dashboard never reads a database older than its code expects.
log migrate
out=$(mktemp)
aws lambda invoke --function-name tideline-run --cli-binary-format raw-in-base64-out \
  --payload '{"task":"migrate"}' "$out" --query FunctionError --output text | grep -qv Unhandled
cat "$out"; echo

log health
url=$(cd infra && terraform output -raw dashboard_url)
code=$(curl -s -o /dev/null -w '%{http_code}' "${url}healthz")
[ "$code" = 200 ] || { echo "healthz answered $code" >&2; exit 1; }
log done
