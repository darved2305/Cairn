-- 0013_observation_namespace_index.sql
-- EXPLAIN on the Day-3 validated/candidate selectors recommended a
-- namespace+lifecycle covering index (0012 was already applied).

CREATE INDEX IF NOT EXISTS observation_by_namespace_lifecycle
  ON trace_observations (namespace_id, lifecycle_state, observed_at DESC)
  STORING (spec_id, trace_digest, semantic_work_key);
