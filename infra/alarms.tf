# One alarm, by email: the scheduled run failed.
#
# The notification path is SNS, not SES and not the app: it works even when
# Tideline itself is what broke. Only ALARM is sent, not the return to OK,
# so a fixed problem does not send a second, confusing email.
#
# There is no "the run did not happen" alarm: CloudWatch alarms look back at
# most seven days, and runs are two weeks apart. The monthly report on the 1st
# is that signal: if it does not arrive, the schedule is the first thing to check.

resource "aws_sns_topic" "alarms" {
  name = "sitewatch-alarms" # kept: renaming would need the email confirmed again
}

resource "aws_sns_topic_subscription" "owen" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
  # AWS emails a confirmation link that has to be clicked once.
}

resource "aws_cloudwatch_metric_alarm" "run_failed" {
  alarm_name          = "tideline-run-failed"
  alarm_description   = "The scheduled Tideline run raised an error. Its log is in /aws/lambda/tideline-run."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.run.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching" # no run this hour is normal, not a failure
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "tideline"
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Each run"
          region = var.region
          stat   = "Maximum"
          period = 86400
          view   = "timeSeries"
          metrics = [
            ["Tideline", "checks_run", { label = "checks run" }],
            ["Tideline", "check_failures", { label = "failing" }],
            ["Tideline", "open_incidents", { label = "open incidents" }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Lambda"
          region = var.region
          stat   = "Sum"
          period = 86400
          view   = "timeSeries"
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", "tideline-run", { label = "run errors" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "tideline-web", { label = "dashboard requests" }],
            ["AWS/Lambda", "Errors", "FunctionName", "tideline-web", { label = "dashboard errors" }],
          ]
        }
      },
    ]
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "tideline-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
