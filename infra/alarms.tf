# Watching the watcher.
#
# If the worker dies, every site looks fine and nobody is told. The heartbeat
# alarm is the answer: the worker publishes `worker_heartbeat` every minute, and
# this alarm fires when that metric is MISSING for 15 minutes. Missing data is
# treated as breaching, which is the whole point; the default (missing data is
# ignored) would keep the alarm green while the worker was dead.
#
# The notification path is SNS, not SES: it does not run on the instance and
# does not depend on Sitewatch working at all.

resource "aws_sns_topic" "alarms" {
  name = "sitewatch-alarms"
}

resource "aws_sns_topic_subscription" "owen" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email
  # AWS emails a confirmation link that has to be clicked once.
}

resource "aws_cloudwatch_metric_alarm" "worker_heartbeat" {
  alarm_name        = "sitewatch-worker-heartbeat-missing"
  alarm_description = "The Sitewatch worker has not published a heartbeat for 10 minutes: checks are not running."

  # The worker publishes a heartbeat every 60 s, so this watches ten one-minute
  # periods rather than three five-minute ones. Measured on 2026-09-17: with
  # 3 x 5 minutes, a stopped worker took 25 minutes to alarm, because CloudWatch
  # waits past the evaluation window for late data before deciding that missing
  # data is really missing. Ten 1-minute periods gives the same tolerance for a
  # single late heartbeat with a much shorter time to detection.
  namespace           = "Sitewatch"
  metric_name         = "worker_heartbeat"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 10
  datapoints_to_alarm = 10
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # a dead worker publishes nothing at all

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name        = "sitewatch-disk-above-80-percent"
  alarm_description = "The instance's root volume is over 80% full. Postgres and Docker images are the usual cause."

  namespace           = "CWAgent"
  metric_name         = "disk_used_percent"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching" # the agent going quiet is the agent alarm's job

  dimensions = {
    InstanceId = aws_instance.app.id
    path       = "/"
    fstype     = "xfs"
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "memory" {
  alarm_name        = "sitewatch-memory-above-85-percent"
  alarm_description = "Sustained high memory on a 2 GB instance. Postgres plus four containers is the usual cause."

  namespace           = "CWAgent"
  metric_name         = "mem_used_percent"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { InstanceId = aws_instance.app.id }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "backup" {
  alarm_name        = "sitewatch-backup-failed"
  alarm_description = "No successful nightly database backup in the last 36 hours."

  namespace           = "Sitewatch"
  metric_name         = "backup_success"
  statistic           = "Sum"
  period              = 43200 # 12 h
  evaluation_periods  = 3     # 36 h
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching" # a backup that never ran publishes nothing

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "check_failures" {
  alarm_name        = "sitewatch-many-check-failures"
  alarm_description = "More than 20 failing checks in 15 minutes: likely Sitewatch's own network or DNS, not 11 sites breaking at once."

  namespace           = "Sitewatch"
  metric_name         = "check_failures"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

# Log group for the containers. Docker's awslogs driver creates it on first use,
# but declaring it here sets the retention, so logs do not accumulate forever.
resource "aws_cloudwatch_log_group" "containers" {
  name              = "/sitewatch/containers"
  retention_in_days = 30
}

resource "aws_cloudwatch_dashboard" "sitewatch" {
  dashboard_name = "sitewatch"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Checks run and failures"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["Sitewatch", "checks_run", { stat = "Sum", label = "checks run" }],
            ["Sitewatch", "check_failures", { stat = "Sum", label = "failures" }],
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Open incidents and worker heartbeat"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["Sitewatch", "open_incidents", { stat = "Maximum", label = "open incidents" }],
            ["Sitewatch", "worker_heartbeat", { stat = "Sum", label = "heartbeats" }],
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Check duration"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["Sitewatch", "check_duration_ms", { stat = "p50", label = "p50" }],
            ["Sitewatch", "check_duration_ms", { stat = "p95", label = "p95" }],
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Host CPU, memory and disk"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/EC2", "CPUUtilization", "InstanceId", aws_instance.app.id, { label = "CPU %" }],
            ["CWAgent", "mem_used_percent", "InstanceId", aws_instance.app.id, { label = "memory %" }],
            ["CWAgent", "disk_used_percent", "InstanceId", aws_instance.app.id, "path", "/", "fstype", "xfs", { label = "disk %" }],
          ]
          period = 300
        }
      },
      {
        type   = "log"
        width  = 24
        height = 6
        properties = {
          title  = "Recent non-ok check results"
          region = var.region
          query  = "SOURCE '/sitewatch/containers' | fields @timestamp, site, check_kind, status, summary | filter event = 'check_result' and status != 'ok' | sort @timestamp desc | limit 50"
          view   = "table"
        }
      },
    ]
  })
}

output "alarm_topic" {
  value = aws_sns_topic.alarms.arn
}

output "cloudwatch_dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/sitewatch"
}
