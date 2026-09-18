# The public demo at tideline.obwebdesign.ca (docs/decisions/0006).
#
# The same image and dashboard as tideline-web, in demo mode: no sign-in,
# read-only, over a showcase database of eight invented businesses. Its role
# can read and write that one file and nothing else: no real data, no secrets,
# no email. Once a day the scheduler has it rebuild the showcase by running the
# real product against a simulated web, so the dates never go stale.

locals {
  showcase_object = "${aws_s3_bucket.data.arn}/showcase.db"
}

resource "aws_iam_role" "demo" {
  name               = "tideline-demo"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "demo_logs" {
  role       = aws_iam_role.demo.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "demo" {
  statement {
    sid       = "TheShowcaseFileOnly"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [local.showcase_object]
  }
  statement {
    sid       = "SeeWhetherItExists"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = ["showcase.db"]
    }
  }
}

resource "aws_iam_role_policy" "demo" {
  name   = "tideline-demo"
  role   = aws_iam_role.demo.id
  policy = data.aws_iam_policy_document.demo.json
}

resource "aws_cloudwatch_log_group" "demo" {
  name              = "/aws/lambda/tideline-demo"
  retention_in_days = 14
}

resource "aws_lambda_function" "demo" {
  function_name = "tideline-demo"
  description   = "The public, read-only Tideline demo over invented businesses."
  role          = aws_iam_role.demo.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 120 # the daily rebuild runs six months of checks (about a minute)

  image_config {
    command = ["tideline.aws_lambda.demo_handler"]
  }

  environment {
    variables = {
      TIDELINE_DEMO_MODE        = "true"
      TIDELINE_DB_BUCKET        = aws_s3_bucket.data.bucket
      TIDELINE_DB_KEY           = "showcase.db"
      TIDELINE_AWS_REGION       = var.region
      TIDELINE_LOG_LEVEL        = "INFO"
      TIDELINE_NOTIFY_CHANNEL   = "log"
      TIDELINE_DISPLAY_TIMEZONE = "America/Vancouver"
    }
  }

  depends_on = [aws_cloudwatch_log_group.demo]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "demo" {
  function_name      = aws_lambda_function.demo.function_name
  authorization_type = "NONE" # public on purpose: invented data, read-only
}

resource "aws_lambda_permission" "demo_url" {
  statement_id           = "PublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.demo.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_iam_role_policy" "scheduler_demo" {
  name = "invoke-the-demo-rebuild"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.demo.arn
    }]
  })
}

resource "aws_scheduler_schedule" "demo_rebuild" {
  name                         = "tideline-demo-rebuild"
  description                  = "Rebuild the demo's invented history each morning, so its dates stay current."
  schedule_expression          = "cron(0 6 * * ? *)"
  schedule_expression_timezone = "America/Vancouver"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.demo.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ task = "showcase" })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

# --- its address ------------------------------------------------------------------

resource "aws_acm_certificate" "demo" {
  provider          = aws.us_east_1
  domain_name       = var.demo_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudfront_distribution" "demo" {
  enabled         = true
  comment         = "Tideline public demo"
  price_class     = "PriceClass_100"
  is_ipv6_enabled = true
  aliases         = var.use_demo_domain ? [var.demo_domain] : []

  origin {
    origin_id   = "tideline-demo"
    domain_name = trimsuffix(trimprefix(aws_lambda_function_url.demo.function_url, "https://"), "/")

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id         = "tideline-demo"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS"] # read-only at the edge too
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # AllViewerExceptHostHeader
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !var.use_demo_domain
    acm_certificate_arn            = var.use_demo_domain ? aws_acm_certificate.demo.arn : null
    ssl_support_method             = var.use_demo_domain ? "sni-only" : null
    minimum_protocol_version       = var.use_demo_domain ? "TLSv1.2_2021" : null
  }
}

output "demo_url" {
  value = var.use_demo_domain ? "https://${var.demo_domain}/" : "https://${aws_cloudfront_distribution.demo.domain_name}/"
}

output "demo_dns_records" {
  description = "Add both at the DNS host: the certificate check, and the demo's address."
  value = concat(
    [for o in aws_acm_certificate.demo.domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }],
    [{ name = var.demo_domain, type = "CNAME", value = aws_cloudfront_distribution.demo.domain_name }],
  )
}
