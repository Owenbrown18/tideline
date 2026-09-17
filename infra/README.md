# infra

Every AWS resource Sitewatch uses, as Terraform. Nothing is created by clicking
in the console.

```bash
aws sso login --profile sitewatch
../scripts/bootstrap_state.sh     # once: creates the S3 bucket for the state file
terraform init
terraform plan                    # read-only, always run this first
terraform apply
```

| File | What it holds |
|---|---|
| `versions.tf` | provider versions, S3 state backend, default tags |
| `variables.tf` | region, instance type, domain, retention |
| `network.tf` | VPC, public subnet, internet gateway, security group (80/443 only, no SSH) |
| `ec2.tf` | the instance, its EBS volume, the Elastic IP, and `user_data.sh.tftpl` |
| `iam_instance.tf` | what the server may do: read its own parameters, pull its image, write logs and metrics, write backups, send mail to Owen only |
| `ecr.tf` | the image repository, keeping the last 10 images |
| `s3.tf` | one private bucket: `backups/` (30 days) and `deploy/` |
| `github_oidc.tf` | the role GitHub Actions assumes, pinned to this repo's `main` |
| `ssm_document.tf` | the one command CI is allowed to run on the server |
| `outputs.tf` | IP, instance id, ECR URL, role ARN, bucket, document name |

Secrets are **not** here. `scripts/put_secrets.sh` writes them to SSM Parameter
Store, so they never enter Terraform state. The deploy reads them on the server.

Applied on 2026-09-17: 24 resources, about USD 20/month (see docs/runbook.md).
