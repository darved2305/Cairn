# EventBridge schedule for the reaper — PROJECT.md §6.3: "30-second
# schedule for the reaper. Bounded takeover latency."
#
# EventBridge's rate() expression has a hard 1-minute minimum granularity
# (confirmed by a real ValidationException from PutRule on
# "rate(30 seconds)" — sub-minute units aren't accepted at all). The fix
# that actually preserves a genuine 30s cadence rather than quietly
# degrading to 60s: EventBridge triggers the Lambda once a minute, and
# lambda/reaper/handler.py itself performs two sweeps 30 seconds apart
# within that one invocation. Sweeps still land every 30s in wall-clock
# time (t=0, 30, 60, 90, ...) — it's just that each EventBridge tick
# produces two of them instead of one.

resource "aws_cloudwatch_event_rule" "reaper_schedule" {
  name                = "${var.project_name}-reaper-schedule"
  description         = "Invoke the lease reaper once a minute; it sweeps twice, 30s apart, per invocation"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "reaper" {
  rule = aws_cloudwatch_event_rule.reaper_schedule.name
  arn  = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reaper_schedule.arn
}
