# Lease reaper — PROJECT.md §4.5. No VPC attachment: CockroachDB Cloud is
# reached over the public internet (see iam.tf's comment on the same
# point), so there's no NAT gateway to pay for just to run a 30-second
# sweep. Run scripts/build_reaper_lambda.sh before `terraform apply` —
# Terraform reads lambda/reaper/reaper.zip, it does not build it.

resource "aws_lambda_function" "reaper" {
  function_name = "${var.project_name}-reaper"
  role          = aws_iam_role.reaper.arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  # events.tf's rule fires once a minute; the handler sweeps twice,
  # REAPER_SWEEP_GAP_SECONDS apart, so the timeout has to comfortably
  # cover gap + two sweeps' own runtime, not just one sweep.
  timeout     = var.reaper_interval_seconds + 15
  memory_size = 256

  filename         = "${path.module}/../lambda/reaper/reaper.zip"
  source_code_hash = filebase64sha256("${path.module}/../lambda/reaper/reaper.zip")

  environment {
    variables = {
      CAIRN_DATABASE_URL_SECRET_ARN = aws_secretsmanager_secret.database_url.arn
      REAPER_SWEEP_GAP_SECONDS      = tostring(var.reaper_interval_seconds)
    }
  }
}

resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/${aws_lambda_function.reaper.function_name}"
  retention_in_days = 14
}
