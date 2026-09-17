#!/bin/bash
# Generates the production secrets and stores them in SSM Parameter Store as
# SecureStrings. Run once (and again to rotate: a deploy picks up new values).
#
# Deliberately not Terraform: values Terraform creates end up in its state file.
# These are only ever read by the instance at deploy time.
set -euo pipefail

REGION=${AWS_REGION:-ca-central-1}
SITES_FILE=${1:-sites.yaml}

put() {
  aws ssm put-parameter --name "$1" --value "$2" --type SecureString \
    --overwrite --region "$REGION" >/dev/null
  echo "wrote $1"
}

if [ ! -f "$SITES_FILE" ]; then
  echo "no $SITES_FILE: copy sites.example.yaml and fill it in" >&2
  exit 1
fi

random() { openssl rand -base64 32 | tr -d '/+=' | cut -c1-40; }

# Only write credentials that do not exist yet, so rerunning this does not
# invalidate the token in Owen's password manager.
ensure() {
  if aws ssm get-parameter --name "$1" --region "$REGION" >/dev/null 2>&1; then
    echo "$1 already set (delete it first to rotate)"
  else
    put "$1" "$(random)"
  fi
}

ensure /sitewatch/db_password
ensure /sitewatch/api_token
ensure /sitewatch/dashboard_password
put /sitewatch/dashboard_user "owen"
# The site list lives here rather than in the repo or the image.
put /sitewatch/sites_yaml "$(cat "$SITES_FILE")"

echo
echo "Read the dashboard password with:"
echo "  aws ssm get-parameter --name /sitewatch/dashboard_password --with-decryption --query Parameter.Value --output text"
