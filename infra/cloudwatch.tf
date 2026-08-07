# Four alarms — PLAN.md §7 D7: "ReuseRate drop, TxnRetries40001 spike,
# reaper failure, quarantine event." PROJECT.md §6.3's five custom EMF
# metrics (ReuseRate, DuplicatesPrevented, FailuresAvoided,
# ProbeLatencyP50, TxnRetries40001) live under the `Cairn` namespace,
# emitted by src/cairn/obs/metrics.py. Only TxnRetries40001 has a real
# call site today (db/txn.py); ReuseRate's alarm and the quarantine-event
# alarm reference metrics whose emitting call sites land on later days
# (the agent loop, contradiction handling) — `treat_missing_data =
# "notBreaching"` means an alarm for a metric nothing emits yet reports
# healthy, not "insufficient data" noise or a false page.

resource "aws_cloudwatch_metric_alarm" "reuse_rate_drop" {
  alarm_name          = "${var.project_name}-reuse-rate-drop"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "ReuseRate"
  namespace           = "Cairn"
  period              = 300
  statistic           = "Average"
  threshold           = 0.3
  treat_missing_data  = "notBreaching"
  alarm_description   = "Reuse rate has dropped below 30% over 3 consecutive 5-minute windows — may indicate a regression in classification/probing, not just an unusually change-heavy period."
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "txn_retries_spike" {
  alarm_name          = "${var.project_name}-txn-retries-spike"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "TxnRetries40001"
  namespace           = "Cairn"
  period              = 300
  statistic           = "Sum"
  threshold           = 50
  treat_missing_data  = "notBreaching"
  alarm_description   = "More than 50 serializable retries in 5 minutes — real contention pattern D2's design.io.txn.py already handles correctly, but a spike this size is worth a human look (PLAN.md §10's skills-driven change #2 target: mean 3.1 -> 0.4 retries per contended claim under normal load)."
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "reaper_failure" {
  alarm_name          = "${var.project_name}-reaper-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "The lease reaper itself errored — this is the built-in Lambda Errors metric, real from the moment the function deploys, not a custom EMF metric waiting on app code."
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.reaper.function_name
  }
}

resource "aws_cloudwatch_metric_alarm" "quarantine_event" {
  alarm_name          = "${var.project_name}-quarantine-event"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "QuarantineEvents"
  namespace           = "Cairn"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "An artifact was quarantined (PROJECT.md §6.5) — deterministic evidence was contradicted after the fact. Always worth paging on; quarantine is a rare, high-signal event, not routine noise."
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}
