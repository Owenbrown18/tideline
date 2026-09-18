# infra

Every AWS resource Tideline uses, as Terraform. Nothing is created by clicking
in the console.

```bash
aws sso login --profile sitewatch
../scripts/bootstrap_state.sh     # once: creates the S3 bucket for the state file
terraform init
terraform plan -var image_tag=<tag>    # read-only, always run this first
terraform apply -var image_tag=<tag>
```

`image_tag` is the image the functions are created with; after that, deploys
(`scripts/deploy.sh`) move them to new images and Terraform leaves the image
alone. Any existing tag works for later plans.

| File | What it holds |
|---|---|
| `versions.tf` | provider versions, S3 state backend, default tags |
| `variables.tf` | region, image tag, domain, secrets path, budget |
| `lambda.tf` | the two functions (run, dashboard), their roles, log groups, the dashboard's function URL, and the 1st-and-15th schedule |
| `cloudfront.tf` | CloudFront in front of the dashboard, its certificate (us-east-1), and a small CloudFront function |
| `s3.tf` | one private bucket: `tideline.db` (versioned, 90 days of earlier copies) and `archive/` |
| `ecr.tf` | the image repository, keeping the last 3 images |
| `ses.tf` | the sending domain (DKIM) and Owen's verified address |
| `alarms.tf` | the alarm email topic, the "run failed" alarm, a CloudWatch dashboard, the USD 3 budget |
| `github_oidc.tf` | the role GitHub Actions assumes, pinned to this repo's `main` |
| `outputs.tf` | the dashboard URL, the DNS records to add, the function URL, the ECR URL |

Secrets are **not** here. `scripts/put_secrets.sh` writes them to SSM Parameter
Store, so they never enter Terraform state. The functions read them when they
start.

Version 1 (an EC2 server, VPC, security group, Elastic IP and EBS volumes) was
applied on 2026-09-17 and destroyed on 2026-09-18 when Tideline moved to Lambda
(docs/decisions/0005).
