# CloudFront in front of the ALB — docs/project/PROJECT.md §6.1: "CloudFront fronts the
# ALB for the public demo URL and TLS." Uses CloudFront's own
# *.cloudfront.net domain and default certificate — no custom domain or
# ACM validation needed for a judged demo. Caching is disabled: this
# fronts a dynamic, database-backed API/console, not static assets, so
# every request should reach the ALB.
#
# count, not a plain resource: brand-new AWS accounts get a real 403
# ("Your account must be verified... contact AWS Support") on
# CreateDistribution until AWS finishes verifying the account — an
# account-level gate with no Terraform-side fix. Defaulting
# create_cloudfront to true keeps this the real target once the account
# clears that gate; setting it false lets every other D7 resource
# (ECS clusters, workers, the reaper, alarms — everything the D7 exit bar
# actually needs) apply cleanly in the meantime instead of failing here
# every single run.

resource "aws_cloudfront_distribution" "console" {
  count       = var.create_cloudfront ? 1 : 0
  enabled     = true
  price_class = "PriceClass_100"
  comment     = "${var.project_name} demo console"

  origin {
    domain_name = aws_lb.console.dns_name
    origin_id   = "console-alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "console-alb"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = true
      headers      = ["*"]
      cookies {
        forward = "all"
      }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

output "demo_url" {
  description = "The public demo URL — docs/project/PLAN.md §7 checklist item. Null until create_cloudfront=true and the AWS account is verified for CloudFront."
  value       = var.create_cloudfront ? "https://${aws_cloudfront_distribution.console[0].domain_name}" : null
}
