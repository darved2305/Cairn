-- 0003_fragments.sql
-- Per-fragment checkpoints for crash recovery (docs/project/PROJECT.md §4.5). Keyed by
-- (work_key, fragment_index) so a resuming worker can look up "what do I
-- already have" without needing to already know a fragment's content
-- digest — that's verified separately against S3 on read
-- (storage/s3.py::get_fragment_verified).

CREATE TABLE run_fragments (
  work_key       STRING NOT NULL,
  fragment_index INT    NOT NULL,
  run_id         UUID   NOT NULL,
  fence          INT8   NOT NULL,
  s3_uri         STRING NOT NULL,
  content_digest STRING NOT NULL,
  duration_ms    INT8   NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (work_key, fragment_index)
);
