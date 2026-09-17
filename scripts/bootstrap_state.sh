#!/bin/bash
# Creates the S3 bucket that holds Terraform state. Run once, before the first
# `terraform init`. Terraform cannot create the bucket its own state lives in.
set -euo pipefail

REGION=${AWS_REGION:-ca-central-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="sitewatch-tfstate-${ACCOUNT}"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "$BUCKET already exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION"
  echo "created $BUCKET"
fi

# Versioning: a bad apply can be rolled back to the previous state file.
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

echo "state bucket ready: $BUCKET"
