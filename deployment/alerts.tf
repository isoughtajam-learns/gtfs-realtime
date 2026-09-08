# CloudWatch alarms + SNS, built in direct response to the v0.3.7 arm64
# crash-loop incident: the ECS service was down for several minutes before
# anyone noticed, because nothing was watching for it. Two failure modes
# covered:
#   1. A service's running task count drops below what it should be
#      (crash-loop, task can't start, etc.) - aws_cloudwatch_metric_alarm.*_task_count
#   2. The underlying EC2 instance itself fails AWS's own health checks
#      (system or instance level) - aws_cloudwatch_metric_alarm.ec2_status_check
#
# LiveTaskCount (AWS/ECS namespace) is used rather than enabling Container
# Insights - it's published natively per-service without that extra
# (billed) opt-in, confirmed available for every service on this cluster.

resource "aws_sns_topic" "alerts" {
  name = "${var.app_name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  # Deploys intentionally drop every service to 0 running tasks briefly -
  # see aws_ecs_service.backend's deployment_minimum_healthy_percent = 0
  # comment (a single instance + fixed host port means the old task must
  # fully stop before the new one starts). Alarms need to tolerate that
  # normal transition (observed live: well under 2 minutes) without firing,
  # while still catching a genuine crash-loop in a reasonable time - the
  # v0.3.7 incident ran for several minutes before it was caught manually.
  task_count_alarm_period             = 60
  task_count_alarm_evaluation_periods = 5
}

resource "aws_cloudwatch_metric_alarm" "backend_task_count" {
  alarm_name        = "${var.app_name}-backend-tasks-down"
  alarm_description = "backend ECS service has run fewer tasks than desired for ${local.task_count_alarm_evaluation_periods} straight minutes - likely a crash-loop."
  namespace         = "AWS/ECS"
  metric_name       = "LiveTaskCount"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.backend.name
  }
  statistic           = "Minimum"
  period              = local.task_count_alarm_period
  evaluation_periods  = local.task_count_alarm_evaluation_periods
  comparison_operator = "LessThanThreshold"
  threshold           = var.backend_desired_count
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "celery_worker_task_count" {
  alarm_name        = "${var.app_name}-celery-worker-tasks-down"
  alarm_description = "celery-worker ECS service has run fewer tasks than desired for ${local.task_count_alarm_evaluation_periods} straight minutes - likely a crash-loop."
  namespace         = "AWS/ECS"
  metric_name       = "LiveTaskCount"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.celery_worker.name
  }
  statistic           = "Minimum"
  period              = local.task_count_alarm_period
  evaluation_periods  = local.task_count_alarm_evaluation_periods
  comparison_operator = "LessThanThreshold"
  threshold           = var.celery_worker_desired_count
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "celery_beat_task_count" {
  alarm_name        = "${var.app_name}-celery-beat-tasks-down"
  alarm_description = "celery-beat ECS service has run fewer tasks than desired for ${local.task_count_alarm_evaluation_periods} straight minutes - scheduled fetches (see src/tasks.py) aren't running."
  namespace         = "AWS/ECS"
  metric_name       = "LiveTaskCount"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.celery_beat.name
  }
  statistic           = "Minimum"
  period              = local.task_count_alarm_period
  evaluation_periods  = local.task_count_alarm_evaluation_periods
  comparison_operator = "LessThanThreshold"
  # celery-beat's desired_count is hardcoded to 1 on the service itself
  # (must never run more than one - see that resource's comment), so this
  # threshold is too rather than reading a variable that doesn't exist.
  threshold          = 1
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "ec2_status_check" {
  alarm_name        = "${var.app_name}-ec2-status-check-failed"
  alarm_description = "The EC2 instance running every ECS task (backend/celery-worker/celery-beat) is failing AWS's own status checks (system or instance level)."
  namespace         = "AWS/EC2"
  metric_name       = "StatusCheckFailed"
  dimensions = {
    InstanceId = aws_instance.ecs.id
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
