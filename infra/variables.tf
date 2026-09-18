variable "region" {
  description = "AWS region. Montreal: Canadian clients' data stays in Canada."
  type        = string
  default     = "ca-central-1"
}

variable "image_tag" {
  description = "The image both Lambda functions start from. Deploys change it with `aws lambda update-function-code` (scripts/deploy.sh), so Terraform only sets it on create."
  type        = string
}

variable "github_repo" {
  description = "owner/name of the repo allowed to deploy through OIDC."
  type        = string
  default     = "Owenbrown18/tideline"
}

variable "dashboard_domain" {
  description = "The dashboard's address. A CNAME at the DNS host points it at CloudFront."
  type        = string
  default     = "status.obwebdesign.ca"
}

variable "use_custom_domain" {
  description = "Serve the dashboard on dashboard_domain. Needs the certificate issued (its validation record at the DNS host) first."
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Where alarm notifications, run summaries and reports go. Owen only, never a client."
  type        = string
  default     = "owenjosephbrown@gmail.com"
}

variable "secrets_prefix" {
  description = "SSM Parameter Store path of the secrets and the site list (scripts/put_secrets.sh)."
  type        = string
  default     = "/sitewatch/"
}

variable "monthly_budget_usd" {
  description = "Owen is emailed when the month's AWS bill heads past this."
  type        = number
  default     = 3
}
