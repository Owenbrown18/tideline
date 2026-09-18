output "dashboard_url" {
  description = "Where the dashboard is: CloudFront's address until use_custom_domain is true."
  value       = var.use_custom_domain ? "https://${var.dashboard_domain}/" : "https://${aws_cloudfront_distribution.dashboard.domain_name}/"
}

output "dns_record_for_the_dashboard" {
  description = "Add at the DNS host: status CNAME to this."
  value = {
    name  = var.dashboard_domain
    type  = "CNAME"
    value = aws_cloudfront_distribution.dashboard.domain_name
  }
}

output "certificate_validation_record" {
  description = "Add at the DNS host once, so ACM can issue the certificate."
  value = [
    for o in aws_acm_certificate.dashboard.domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ]
}

output "certificate_status" {
  value = aws_acm_certificate.dashboard.status
}

output "function_url" {
  description = "The dashboard function itself, behind CloudFront."
  value       = aws_lambda_function_url.web.function_url
}

output "ecr_repository" {
  description = "Where deploys push the image."
  value       = aws_ecr_repository.app.repository_url
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE variable in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "bucket" {
  description = "tideline.db (versioned), and the final Postgres backup under archive/."
  value       = aws_s3_bucket.data.bucket
}
