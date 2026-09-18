# CloudFront puts the dashboard on status.obwebdesign.ca with HTTPS.
#
# A Lambda function URL cannot have a custom domain itself, so CloudFront sits
# in front: it holds the certificate for status.obwebdesign.ca and passes each
# request on to the function URL. Nothing is cached (every page is live data
# behind a password). CloudFront's always-free allowance (1 TB and 10 million
# requests a month) covers this many times over.
#
# The certificate must be in us-east-1, where CloudFront reads certificates
# from. ACM proves the domain is Owen's with one DNS record at Hostinger
# (output `certificate_validation_record`). Until that record exists and the
# certificate is issued, `use_custom_domain` stays false and the dashboard is
# reachable on CloudFront's own address.

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = {
      Project   = "tideline"
      ManagedBy = "terraform"
      Repo      = "github.com/${var.github_repo}"
    }
  }
}

resource "aws_acm_certificate" "dashboard" {
  provider          = aws.us_east_1
  domain_name       = var.dashboard_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  function_url_host = trimsuffix(trimprefix(aws_lambda_function_url.web.function_url, "https://"), "/")
}

resource "aws_cloudfront_function" "restore_login_prompt" {
  name    = "tideline-restore-login-prompt"
  runtime = "cloudfront-js-2.0"
  comment = "Puts back the WWW-Authenticate header Lambda renames, so browsers ask for the password."
  publish = true
  code    = file("${path.module}/functions/restore_login_prompt.js")
}

resource "aws_cloudfront_distribution" "dashboard" {
  enabled         = true
  comment         = "Tideline dashboard"
  price_class     = "PriceClass_100" # North America and Europe edges only
  is_ipv6_enabled = true
  aliases         = var.use_custom_domain ? [var.dashboard_domain] : []

  origin {
    origin_id   = "tideline-web"
    domain_name = local.function_url_host

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "tideline-web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed policies: cache nothing, and forward everything the browser
    # sent (including the Authorization header basic auth needs) except Host,
    # which must be the function URL's own.
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # AllViewerExceptHostHeader

    function_association {
      event_type   = "viewer-response"
      function_arn = aws_cloudfront_function.restore_login_prompt.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !var.use_custom_domain
    acm_certificate_arn            = var.use_custom_domain ? aws_acm_certificate.dashboard.arn : null
    ssl_support_method             = var.use_custom_domain ? "sni-only" : null
    minimum_protocol_version       = var.use_custom_domain ? "TLSv1.2_2021" : null
  }
}
