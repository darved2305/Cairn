-- 0012_flight_console_grants.sql
-- Read-only console access to Flight Recorder tables (Day 3).
-- Redacted resource refs stay out of the console role: grant tables that
-- carry digests/state, not raw path material beyond what reuse_decisions
-- already exposes via explanation. GRANT is idempotent.

GRANT SELECT ON TABLE namespaces              TO cairn_console_ro;
GRANT SELECT ON TABLE execution_specs         TO cairn_console_ro;
GRANT SELECT ON TABLE trace_contents          TO cairn_console_ro;
GRANT SELECT ON TABLE trace_observations      TO cairn_console_ro;
GRANT SELECT ON TABLE content_blobs           TO cairn_console_ro;
GRANT SELECT ON TABLE work_heads              TO cairn_console_ro;
GRANT SELECT ON TABLE work_generations        TO cairn_console_ro;
GRANT SELECT ON TABLE derivations             TO cairn_console_ro;
GRANT SELECT ON TABLE work_subscribers        TO cairn_console_ro;
GRANT SELECT ON TABLE reuse_rule_revisions    TO cairn_console_ro;
GRANT SELECT ON TABLE reuse_rule_heads        TO cairn_console_ro;

-- Selector indexes already land in 0010; keep an explicit covering index
-- for reverse invalidation by blob_digest (EXPLAIN'd on Day 3).
CREATE INDEX IF NOT EXISTS derivations_reverse_blob
  ON derivations (blob_digest, namespace_id)
  STORING (semantic_work_key, generation, state, observation_id);
