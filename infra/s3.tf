# Content-addressed artifact + fragment storage — docs/project/PROJECT.md §4.2/§4.5,
# storage/s3.py. One bucket, prefix-scoped by kind (artifacts/fragments/
# datasets/models); IAM (iam.tf) grants the worker role access scoped to
# these specific prefixes, not the whole bucket.
#
# Security (docs/project/PLAN.md §10 / "no simulation" audit): block-public-access on
# every axis, default SSE-S3 encryption, versioning on so an accidental
# overwrite of a fixed-key object (put_bytes, not put_content_addressed)
# is recoverable.

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Content-addressed objects are already immutable in practice (the key IS
# the content hash), so an unbounded object count is fine at demo scale;
# fragments and old noncurrent versions are cheap enough not to need a
# lifecycle rule for a deployment this short-lived. Revisit if this bucket
# ever holds more than a demo's worth of data.
