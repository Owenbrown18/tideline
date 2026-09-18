# Terraform state lives in S3 (versioned, encrypted, private) so it is not on
# one laptop. The bucket is created before the first `terraform init` by
# scripts/bootstrap_state.sh, because Terraform cannot store its own state in a
# bucket it has not created yet.
#
# use_lockfile: S3 handles the state lock, so no DynamoDB table is needed.

terraform {
  required_version = ">= 1.9"

  backend "s3" {
    bucket       = "sitewatch-tfstate-053578820490"
    key          = "sitewatch/terraform.tfstate"
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "tideline"
      ManagedBy = "terraform"
      Repo      = "github.com/${var.github_repo}"
    }
  }
}
