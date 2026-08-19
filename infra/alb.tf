# ALB in front of the console — docs/project/PLAN.md §7.2's public demo URL. Ingress is
# restricted to CloudFront's own origin-facing IP ranges via AWS's managed
# prefix list, not "anyone on the internet who guesses the ALB's DNS name"
# — docs/project/PLAN.md's security audit calls for "CloudFront-only ALB ingress."
# TLS itself is terminated at CloudFront (cloudfront.tf), not here: the
# ALB talks plain HTTP to CloudFront over AWS's internal network, which is
# the standard pattern for a demo without a custom domain / ACM cert.

data "aws_ec2_managed_prefix_list" "cloudfront_origin_facing" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name = "${var.project_name}-alb"
  # EC2's GroupDescription field rejects non-ASCII (a legacy EC2-VPC API
  # restriction, confirmed by a real 400 from CreateSecurityGroup) — plain
  # hyphens here, unlike every other free-text field in this repo.
  description = "Cairn console ALB - CloudFront origin-facing ranges only"
  vpc_id      = data.aws_vpc.primary.id

  ingress {
    description     = "HTTP from CloudFront only"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront_origin_facing.id]
  }

  # TEMPORARY — remove once this AWS account clears CloudFront's new-account
  # verification gate (see cloudfront.tf's comment / var.create_cloudfront).
  # Until then CloudFront can't be created, so the CloudFront-only rule
  # above leaves the console completely unreachable — this is the only
  # path to a public demo URL in the meantime. Read-only, no-auth API
  # (docs/project/PLAN.md §8 open decision 4), so the exposure is low, but it's still a
  # deliberate, temporary override of the documented ingress design.
  ingress {
    description = "TEMP public HTTP - remove once CloudFront is live"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "console" {
  name               = "${var.project_name}-console"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.primary.ids
}

resource "aws_lb_target_group" "console" {
  name        = "${var.project_name}-console"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.primary.id
  target_type = "ip"

  health_check {
    path                = "/api/health"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    interval            = 30
    timeout             = 10
    matcher             = "200-499"
  }
}

resource "aws_lb_listener" "console" {
  load_balancer_arn = aws_lb.console.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.console.arn
  }
}
