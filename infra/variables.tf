variable "region" {
  description = "AWS region. Montreal: Canadian clients' data stays in Canada."
  type        = string
  default     = "ca-central-1"
}

variable "instance_type" {
  description = "Graviton (arm64) instance. t4g.small is 2 vCPU / 2 GiB."
  type        = string
  default     = "t4g.small"
}

variable "root_volume_gb" {
  description = "Root EBS volume size. Holds the OS, images, Postgres data and logs."
  type        = number
  default     = 20
}

variable "github_repo" {
  description = "owner/name of the repo allowed to deploy through OIDC."
  type        = string
  default     = "Owenbrown18/tideline"
}

variable "dashboard_domain" {
  description = "Hostname Caddy gets a certificate for."
  type        = string
  default     = "status.obwebdesign.ca"
}

variable "alert_email" {
  description = "Where SNS alarm notifications go. Owen only, never a client."
  type        = string
  default     = "owenjosephbrown@gmail.com"
}

variable "backup_retention_days" {
  description = "How long nightly pg_dump files are kept in S3."
  type        = number
  default     = 30
}
