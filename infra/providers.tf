# Terraform + provider configuration — PLAN.md D7.
#
# State is local (terraform.tfstate, gitignored) rather than an S3 backend:
# this is a single-operator hackathon deployment, not a team one, and a
# remote backend would be one more piece of infrastructure to bootstrap
# and tear down correctly. `make teardown` / `terraform destroy` is what
# actually matters for cost control here, not state locking.
#
# Two providers, not one: us-east-1 is the default/primary (console, ALB,
# CloudFront, Bedrock, the primary worker service); us-west-2 is aliased
# and used only for the second worker service, which is the whole point —
# PROJECT.md §4.2's claim race has to be a real cross-region race against
# one CockroachDB cluster, not two processes in one region.

terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region_primary

  default_tags {
    tags = {
      Project   = "cairn"
      ManagedBy = "terraform"
    }
  }
}

provider "aws" {
  alias  = "secondary"
  region = var.region_secondary

  default_tags {
    tags = {
      Project   = "cairn"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
