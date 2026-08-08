# Variables — PLAN.md D7. Defaults match PLAN.md/PROJECT.md's locked
# environment decisions (§2, §6.3); nothing here should need overriding
# for the judged demo, only for local iteration.

variable "region_primary" {
  description = "Primary AWS region — console, ALB, CloudFront, Bedrock, worker A."
  type        = string
  default     = "us-east-1"
}

variable "region_secondary" {
  description = "Secondary AWS region — worker B, for the real cross-region claim race."
  type        = string
  default     = "us-west-2"
}

variable "project_name" {
  description = "Prefix for all named resources."
  type        = string
  default     = "cairn"
}

variable "environment" {
  description = "Deployment environment tag."
  type        = string
  default     = "demo"
}

variable "cairn_database_url" {
  description = "CockroachDB Cloud connection string. Never has a default — must be supplied via TF_VAR_cairn_database_url or a .auto.tfvars file that is gitignored, and is stored only in Secrets Manager, never in a container image or task definition env var."
  type        = string
  sensitive   = true
}

variable "cairn_console_database_url" {
  description = "The console's OWN read-only (cairn_console_ro) CockroachDB connection string — scripts/provision_console_role.py prints it. Never has a default, same handling as cairn_database_url."
  type        = string
  sensitive   = true
}

variable "cairn_approval_usd" {
  description = "Dollar threshold above which the agent escalates instead of launching (action 9, ESCALATE) — PROJECT.md §6.4."
  type        = number
  default     = 0.50
}

variable "cairn_mode" {
  description = "conservative (default, judged) | balanced (documented, opt-in, never used in the demo) — PROJECT.md §3.2."
  type        = string
  default     = "conservative"

  validation {
    condition     = contains(["conservative", "balanced"], var.cairn_mode)
    error_message = "cairn_mode must be 'conservative' or 'balanced'."
  }
}

variable "container_image_tag" {
  description = "Tag on the ECR image to deploy — set by CI to the commit SHA; 'latest' for manual iteration."
  type        = string
  default     = "latest"
}

variable "worker_cpu" {
  description = "Fargate vCPU units for a worker task (1024 = 1 vCPU) — PROJECT.md §6.3: 2 vCPU."
  type        = number
  default     = 2048
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for a worker task — PROJECT.md §6.3: 4 GiB."
  type        = number
  default     = 4096
}

variable "console_cpu" {
  description = "Fargate vCPU units for the console task."
  type        = number
  default     = 512
}

variable "console_memory" {
  description = "Fargate memory (MiB) for the console task."
  type        = number
  default     = 1024
}

variable "bedrock_claude_model_id" {
  description = "Bedrock model ID for agent reasoning — PLAN.md §2."
  type        = string
  default     = "anthropic.claude-sonnet-5"
}

variable "bedrock_titan_model_id" {
  description = "Bedrock model ID for negative-memory embeddings — PLAN.md §2."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "reaper_interval_seconds" {
  description = "EventBridge schedule for the lease reaper — PROJECT.md §4.5: bounded takeover latency."
  type        = number
  default     = 30
}

variable "create_cloudfront" {
  description = "Set true once this AWS account clears CloudFront's new-account verification gate (see cloudfront.tf's comment) — a real 403 from CreateDistribution, not a config error. Defaults false so the rest of D7's infrastructure applies cleanly while that's pending."
  type        = bool
  default     = false
}

variable "alarm_actions" {
  description = "SNS topic ARNs (or other CloudWatch alarm action targets) to notify — empty by default so the demo doesn't require a pre-existing notification channel; alarms still fire and show in the console either way."
  type        = list(string)
  default     = []
}
