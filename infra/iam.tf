# Least-privilege IAM — PLAN.md §7 D7 exit bar and PROJECT.md §6.3: "S3
# prefix-scoped, bedrock:InvokeModel on two model ARNs only." Every role
# below is scoped to exactly what its process needs; nothing gets
# AdministratorAccess or a wildcard resource unless the action genuinely
# has no resource-level permission support (e.g. cloudwatch:PutMetricData).

# ---------------------------------------------------------------------------
# ECS task execution role — pulls the image, writes container logs, reads
# the one secret every task needs injected at launch. Shared by worker and
# console tasks; this is infrastructure plumbing, not app-level access.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${var.project_name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid       = "ReadDatabaseUrlSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database_url.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "read-database-url-secret"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# ---------------------------------------------------------------------------
# Worker task role — the role the *container* assumes at runtime.
# S3 prefix-scoped to exactly the prefixes storage/s3.py and
# scripts/vendor_dataset.py actually write to; Bedrock scoped to exactly
# the two model ARNs PROJECT.md names, nothing else.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "worker_task" {
  name               = "${var.project_name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

locals {
  # Every prefix put_content_addressed/put_bytes/fragment_key actually use
  # (workload/stage_*.py, storage/s3.py, scripts/vendor_dataset.py) — not a
  # bucket-wide grant.
  s3_object_prefixes = [
    "dataset/*",
    "features/*",
    "checkpoint/*",
    "eval/*",
    "fragments/*",
    "datasets/*",
    "models/*",
  ]

  bedrock_model_arns = [
    "arn:aws:bedrock:${var.region_primary}::foundation-model/${var.bedrock_claude_model_id}",
    "arn:aws:bedrock:${var.region_primary}::foundation-model/${var.bedrock_titan_model_id}",
  ]

  # `bedrock:InvokeModel` alone is NOT sufficient for the Claude calls this
  # codebase actually makes. classify/llm.py and console/inspector.py both use
  # `AnthropicBedrockMantle` — PLAN.md §2's locked call shape, required because
  # Sonnet 5's `thinking`/`output_config` parameters are only accepted through
  # that client — and that client targets the Bedrock *Mantle* endpoint, whose
  # authorization action is `bedrock-mantle:CreateInference` on a project ARN,
  # not `bedrock:InvokeModel` on a foundation-model ARN.
  #
  # Found empirically, not theorised: an IAM principal holding only
  # InvokeModel gets `AccessDeniedException ... not authorized to perform:
  # bedrock-mantle:CreateInference on resource: arn:aws:bedrock-mantle:
  # <region>:<account>:project/default`. Without this statement the deployed
  # console's Memory Inspector would 403 on every question while every other
  # panel worked — the worst possible failure shape for a judged demo.
  #
  # Titan embeddings keep using the classic InvokeModel path (embeddings.py
  # calls bedrock-runtime directly), so both statements are needed, not one.
  bedrock_mantle_project_arn = "arn:aws:bedrock-mantle:${var.region_primary}:${data.aws_caller_identity.current.account_id}:project/default"
}

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid     = "ArtifactObjectAccess"
    actions = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
    resources = [
      for prefix in local.s3_object_prefixes : "${aws_s3_bucket.artifacts.arn}/${prefix}"
    ]
  }

  statement {
    sid       = "ListOwnBucketOnly"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn]
  }

  statement {
    sid       = "BedrockTwoModelsOnly"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_model_arns
  }

  statement {
    sid       = "BedrockMantleClaudeOnly"
    actions   = ["bedrock-mantle:CreateInference"]
    resources = [local.bedrock_mantle_project_arn]
  }

  statement {
    sid       = "EmfCustomMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData has no resource-level ARN support
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Cairn"]
    }
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "worker-least-privilege"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}

# ---------------------------------------------------------------------------
# Console task role — read-only against the app's own domain; enforcement
# of "read-only" lives at the database layer (a read-only SQL role) per
# PLAN.md §8 open decision 4, "not just in the UI." The AWS-side role only
# needs Bedrock for the Memory Inspector agent (D9).
# ---------------------------------------------------------------------------

resource "aws_iam_role" "console_task" {
  name               = "${var.project_name}-console-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "console_task" {
  statement {
    sid       = "BedrockTwoModelsOnly"
    actions   = ["bedrock:InvokeModel"]
    resources = local.bedrock_model_arns
  }

  # See the local's comment: the Memory Inspector agent
  # (console/inspector.py) reaches Claude through AnthropicBedrockMantle, so
  # InvokeModel by itself is not enough for the panel to work.
  statement {
    sid       = "BedrockMantleClaudeOnly"
    actions   = ["bedrock-mantle:CreateInference"]
    resources = [local.bedrock_mantle_project_arn]
  }
}

resource "aws_iam_role_policy" "console_task" {
  name   = "console-least-privilege"
  role   = aws_iam_role.console_task.id
  policy = data.aws_iam_policy_document.console_task.json
}

# ---------------------------------------------------------------------------
# Reaper Lambda role — CloudWatch Logs only, plus the same secret read as
# the ECS execution role. No VPC attachment (see lambda.tf): CockroachDB
# Cloud is reached over the public internet, so no NAT gateway is needed
# just to run a lease-expiry sweep every 30 seconds.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reaper" {
  name               = "${var.project_name}-reaper"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "reaper_logs" {
  role       = aws_iam_role.reaper.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "reaper_secrets" {
  statement {
    sid       = "ReadDatabaseUrlSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database_url.arn]
  }
}

resource "aws_iam_role_policy" "reaper_secrets" {
  name   = "read-database-url-secret"
  role   = aws_iam_role.reaper.id
  policy = data.aws_iam_policy_document.reaper_secrets.json
}
