# One image, one repo — PLAN.md §6.1: "One image, one deploy path." The
# worker and console tasks (ecs.tf) run the same image with different
# container commands, not separate images.

resource "aws_ecr_repository" "cairn" {
  name                 = var.project_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "cairn" {
  repository = aws_ecr_repository.cairn.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "expire untagged images after 3 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}
