# ECS Fargate — PROJECT.md §6.3: "Runs the real workload as tasks in two
# regions (us-east-1, us-west-2) and hosts the console." Uses each
# region's default VPC/subnets rather than a hand-rolled network stack —
# this is a demo cluster meant to be torn down after judging, not a
# production network topology, and the default VPC already gives every
# Fargate task a public IP and internet route, which is all it needs to
# reach CockroachDB Cloud, S3, ECR, and Bedrock.
#
# Workers are NOT a persistent ECS Service: PROJECT.md's own demo script
# launches "worker A (us-east-1) and worker B (us-west-2) within 300ms of
# each other" on demand for one race, not as an always-on process burning
# Fargate-hours 24/7. scripts/race.py and scripts/kill_worker.sh (D7) call
# ECS RunTask directly against the task definitions below. The console
# *is* a persistent Service — it's the public demo URL.

# --- default VPC/subnets, one data source per region ---

data "aws_vpc" "primary" {
  default = true
}

data "aws_subnets" "primary" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.primary.id]
  }
}

data "aws_vpc" "secondary" {
  provider = aws.secondary
  default  = true
}

data "aws_subnets" "secondary" {
  provider = aws.secondary
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.secondary.id]
  }
}

# --- clusters ---

resource "aws_ecs_cluster" "primary" {
  name = "${var.project_name}-${var.region_primary}"
}

resource "aws_ecs_cluster" "secondary" {
  provider = aws.secondary
  name     = "${var.project_name}-${var.region_secondary}"
}

# --- security groups: egress-only for workers (no inbound needed — they
# are launched tasks, not addressable services); console additionally
# accepts inbound from the ALB on its app port. ---

resource "aws_security_group" "worker_primary" {
  name        = "${var.project_name}-worker"
  description = "Cairn worker tasks - us-east-1" # EC2 GroupDescription rejects non-ASCII
  vpc_id      = data.aws_vpc.primary.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "worker_secondary" {
  provider    = aws.secondary
  name        = "${var.project_name}-worker"
  description = "Cairn worker tasks - us-west-2" # EC2 GroupDescription rejects non-ASCII
  vpc_id      = data.aws_vpc.secondary.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "console" {
  name        = "${var.project_name}-console"
  description = "Cairn console task - accepts ALB traffic only" # EC2 GroupDescription rejects non-ASCII
  vpc_id      = data.aws_vpc.primary.id

  ingress {
    description     = "from the ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- log groups ---

resource "aws_cloudwatch_log_group" "worker_primary" {
  name              = "/ecs/${var.project_name}/worker-${var.region_primary}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker_secondary" {
  provider          = aws.secondary
  name              = "/ecs/${var.project_name}/worker-${var.region_secondary}"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "console" {
  name              = "/ecs/${var.project_name}/console"
  retention_in_days = 14
}

# --- the one secret every task reads at launch ---

resource "aws_secretsmanager_secret" "database_url" {
  name        = "${var.project_name}/database-url"
  description = "CockroachDB Cloud connection string — PLAN.md §7 D7."
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = var.cairn_database_url
}

# --- the console's OWN, read-only credential ------------------------------
#
# PLAN.md §8 open decision 4 and D10's security checklist: the console must
# not share the workers' write-capable database credential. Today it does —
# `local.common_secrets` below is injected into all three task definitions —
# and that is the single largest security gap in this stack, because the
# console is the one task fronted by a public, unauthenticated URL.
#
# The database half of the fix is already built and committed:
#   db/migrations/0008_console_readonly_role.sql   -> cairn_console_ro (SELECT only)
#   scripts/provision_console_role.py              -> the login user + verification
#
# The Terraform half is the block below. It is commented out rather than
# applied because it needs a new `cairn_console_database_url` variable holding
# the URL that script prints, and a `terraform apply` is a real-infra action
# reserved for an explicit human decision — not something a code change should
# smuggle in. To enable, in order:
#
#   1. make migrate                                  (applies 0008)
#   2. uv run python scripts/provision_console_role.py
#      -> prints the cairn_console URL and proves the role cannot write
#   3. add `variable "cairn_console_database_url" { type = string; sensitive = true }`
#      to infra/vars.tf, and set it in your tfvars from step 2's output
#   4. uncomment this block
#   5. in aws_ecs_task_definition.console below, swap
#        secrets = local.common_secrets
#      for
#        secrets = local.console_secrets
#   6. add the new ARN to data.aws_iam_policy_document.ecs_execution_secrets
#      in infra/iam.tf so the execution role can read it
#   7. terraform apply
#
# After that the public console physically cannot write to CockroachDB, and
# "read-only" stops being a property of console/queries.py's contents.
#
# resource "aws_secretsmanager_secret" "console_database_url" {
#   name        = "${var.project_name}/console-database-url"
#   description = "Read-only (SELECT-only) CockroachDB role for the public console."
# }
#
# resource "aws_secretsmanager_secret_version" "console_database_url" {
#   secret_id     = aws_secretsmanager_secret.console_database_url.id
#   secret_string = var.cairn_console_database_url
# }

# --- task definitions ---

locals {
  image = "${aws_ecr_repository.cairn.repository_url}:${var.container_image_tag}"

  common_env = [
    { name = "CAIRN_MODE", value = var.cairn_mode },
    { name = "CAIRN_APPROVAL_USD", value = tostring(var.cairn_approval_usd) },
    { name = "CAIRN_AWS_REGION", value = var.region_primary },
  ]

  common_secrets = [
    { name = "CAIRN_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
  ]

  # Swap this in for the console task once the read-only role is provisioned —
  # see the long comment on the console_database_url secret above.
  #
  # console_secrets = [
  #   { name = "CAIRN_DATABASE_URL", valueFrom = aws_secretsmanager_secret.console_database_url.arn },
  # ]
}

resource "aws_ecs_task_definition" "worker_primary" {
  family                   = "${var.project_name}-worker-${var.region_primary}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["cairn", "--help"] # overridden per-invocation by RunTask
    environment = concat(local.common_env, [
      { name = "CAIRN_WORKER_REGION", value = var.region_primary },
    ])
    secrets = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker_primary.name
        awslogs-region        = var.region_primary
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker_secondary" {
  provider                 = aws.secondary
  family                   = "${var.project_name}-worker-${var.region_secondary}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["cairn", "--help"] # overridden per-invocation by RunTask
    environment = concat(local.common_env, [
      { name = "CAIRN_WORKER_REGION", value = var.region_secondary },
    ])
    secrets = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker_secondary.name
        awslogs-region        = var.region_secondary
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "console" {
  family                   = "${var.project_name}-console"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.console_cpu
  memory                   = var.console_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.console_task.arn

  container_definitions = jsonencode([{
    name      = "console"
    image     = local.image
    essential = true
    # The real console server. One image, one deploy path (PROJECT.md §6.1):
    # this is the same image the workers run, with a different command, and
    # `cairn.console.api` serves both the JSON API and the built React SPA
    # baked in at /app/src/cairn/console/static (see Dockerfile).
    command      = ["uvicorn", "cairn.console.api:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.common_env
    # TODO(read-only role): change to local.console_secrets once
    # scripts/provision_console_role.py has been run and the commented
    # console_database_url secret above is enabled. Until then the console
    # shares the workers' write-capable credential and "read-only" is
    # enforced only by console/queries.py containing nothing but SELECTs.
    secrets = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.console.name
        awslogs-region        = var.region_primary
        awslogs-stream-prefix = "console"
      }
    }
  }])
}

# The ECS *service* — D8's real FastAPI console command (above) now
# stays up, so a persistent Service with desired_count=1 is safe: it
# won't restart-loop, and the target group has something to actually
# health-check.
resource "aws_ecs_service" "console" {
  name            = "${var.project_name}-console"
  cluster         = aws_ecs_cluster.primary.id
  task_definition = aws_ecs_task_definition.console.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = data.aws_subnets.primary.ids
    security_groups  = [aws_security_group.console.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.console.arn
    container_name   = "console"
    container_port   = 8000
  }
  depends_on = [aws_lb_listener.console]
}
