# Two Lambda functions from one container image (docs/decisions/0005):
#
#   tideline-run   checks every site; started by EventBridge Scheduler on the
#                  1st and 15th at 07:00 Pacific
#   tideline-web   the dashboard and API; a function URL, behind CloudFront
#
# Each has its own role with only what it needs. Neither is in a VPC: they
# only make outbound HTTPS requests, and a VPC would need a NAT gateway
# (about USD 35 a month) to reach the internet.

locals {
  image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  db_object = "${aws_s3_bucket.data.arn}/tideline.db"
  common_env = {
    TIDELINE_DB_BUCKET        = aws_s3_bucket.data.bucket
    TIDELINE_DB_KEY           = "tideline.db"
    TIDELINE_SECRETS_PREFIX   = var.secrets_prefix
    TIDELINE_AWS_REGION       = var.region
    TIDELINE_PUBLIC_URL       = "https://${var.dashboard_domain}"
    TIDELINE_LOG_LEVEL        = "INFO"
    TIDELINE_ALERT_EMAIL      = var.alert_email
    TIDELINE_DISPLAY_TIMEZONE = "America/Vancouver"
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- what both functions need ----------------------------------------------------

data "aws_iam_policy_document" "database_and_secrets" {
  statement {
    sid       = "TheDatabaseFile"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = [local.db_object]
  }
  statement {
    # Without ListBucket, S3 answers "access denied" instead of "not found" for
    # a missing file, and the very first run could not tell the difference.
    sid       = "SeeWhetherTheDatabaseExists"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = ["tideline.db"]
    }
  }
  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.secrets_prefix}*"]
  }
  statement {
    sid       = "DecryptThemViaSsmOnly"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }
}

# --- the run -----------------------------------------------------------------------

resource "aws_iam_role" "run" {
  name               = "tideline-run"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "run_logs" {
  role       = aws_iam_role.run.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "run" {
  name   = "tideline-run"
  role   = aws_iam_role.run.id
  policy = data.aws_iam_policy_document.run.json
}

data "aws_iam_policy_document" "run" {
  source_policy_documents = [data.aws_iam_policy_document.database_and_secrets.json]
  statement {
    # Even if the code were tricked into sending mail, it can only send to Owen.
    sid       = "SendMailToOwenOnly"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["*"]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "ses:Recipients"
      values   = [var.alert_email]
    }
  }
}

resource "aws_cloudwatch_log_group" "run" {
  name              = "/aws/lambda/tideline-run"
  retention_in_days = 90
}

resource "aws_lambda_function" "run" {
  function_name = "tideline-run"
  description   = "Checks every site once. Runs on the 1st and 15th."
  role          = aws_iam_role.run.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["arm64"]
  memory_size   = 512
  # A full run measured 26 s (86 checks, 2026-09-18); 10 minutes is headroom
  # for a slow site retrying, well under Lambda's 15-minute limit.
  timeout = 600

  image_config {
    command = ["tideline.aws_lambda.run_handler"]
  }

  environment {
    variables = merge(local.common_env, { TIDELINE_NOTIFY_CHANNEL = "ses" })
  }

  depends_on = [aws_cloudwatch_log_group.run]

  lifecycle {
    ignore_changes = [image_uri] # deploys update the image, not Terraform
  }
}

# --- the schedule -----------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "tideline-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-the-run"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.run.arn
    }]
  })
}

resource "aws_scheduler_schedule" "run" {
  name        = "tideline-run"
  description = "Check every site on the 1st and 15th; the run on the 1st also sends last month's reports."
  # 07:00 in Vancouver. Scheduler follows the time zone, so the run stays at
  # 07:00 local through any clock change.
  schedule_expression          = "cron(0 7 1,15 * ? *)"
  schedule_expression_timezone = "America/Vancouver"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.run.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ task = "run" })

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

# --- the dashboard ------------------------------------------------------------------

resource "aws_iam_role" "web" {
  name               = "tideline-web"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "web_logs" {
  role       = aws_iam_role.web.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "web" {
  # Reads and writes the database file (the "accept DNS change" button), reads
  # its secrets. No email: the dashboard never sends anything.
  name   = "tideline-web"
  role   = aws_iam_role.web.id
  policy = data.aws_iam_policy_document.database_and_secrets.json
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/tideline-web"
  retention_in_days = 30
}

resource "aws_lambda_function" "web" {
  function_name = "tideline-web"
  description   = "The Tideline dashboard and API, behind CloudFront."
  role          = aws_iam_role.web.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["arm64"]
  memory_size   = 512
  timeout       = 20

  image_config {
    command = ["tideline.aws_lambda.web_handler"]
  }

  environment {
    variables = merge(local.common_env, { TIDELINE_NOTIFY_CHANNEL = "log" })
  }

  depends_on = [aws_cloudwatch_log_group.web]

  lifecycle {
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "web" {
  function_name = aws_lambda_function.web.function_name
  # Public, like any website: the app itself asks for the dashboard password
  # (basic auth) or the API token on every request. CloudFront's own signing
  # (OAC) is not used because it replaces the Authorization header that basic
  # auth needs.
  authorization_type = "NONE"
}

data "aws_caller_identity" "current" {}

resource "aws_lambda_permission" "web_url" {
  statement_id           = "PublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.web.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
