output "public_ip" {
  description = "Point the DNS A record for the dashboard at this address."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  description = "For SSM: aws ssm start-session --target <this>"
  value       = aws_instance.app.id
}

output "ecr_repository" {
  description = "Where CI pushes the image."
  value       = aws_ecr_repository.app.repository_url
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE secret (or variable) in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "bucket" {
  description = "Backups under backups/, deploy files under deploy/."
  value       = aws_s3_bucket.data.bucket
}

output "deploy_document" {
  description = "The one SSM document CI may run."
  value       = aws_ssm_document.deploy.name
}

output "dashboard_url" {
  value = "https://${var.dashboard_domain}/"
}
