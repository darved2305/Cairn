# Everything scripts/race.py --ecs and scripts/kill_worker.sh need to
# actually invoke ECS RunTask — worker security groups and a subnet exist
# in Terraform state but were otherwise only discoverable by hand-digging
# through the AWS console, which defeats the point of scripting the demo.

output "cluster_primary" {
  value = aws_ecs_cluster.primary.name
}

output "cluster_secondary" {
  value = aws_ecs_cluster.secondary.name
}

output "worker_task_definition_primary" {
  value = aws_ecs_task_definition.worker_primary.family
}

output "worker_task_definition_secondary" {
  value = aws_ecs_task_definition.worker_secondary.family
}

output "worker_security_group_primary" {
  value = aws_security_group.worker_primary.id
}

output "worker_security_group_secondary" {
  value = aws_security_group.worker_secondary.id
}

# First subnet in each region's default VPC — sufficient for a demo
# worker task, which needs exactly one subnet to launch into, not a
# specific AZ.
output "subnet_primary" {
  value = data.aws_subnets.primary.ids[0]
}

output "subnet_secondary" {
  value = data.aws_subnets.secondary.ids[0]
}

output "ecr_repository_url" {
  value = aws_ecr_repository.cairn.repository_url
}

output "reaper_function_name" {
  value = aws_lambda_function.reaper.function_name
}
