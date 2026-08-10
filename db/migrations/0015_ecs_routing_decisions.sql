-- 0015_ecs_routing_decisions.sql
-- Persists one planner ECS-routing decision that consumed normalized
-- ``ccloud cluster info`` topology. Stale / unknown / non-AWS topology
-- must fail closed in application code — this table only stores authorized
-- decisions that already passed that gate.

CREATE TABLE IF NOT EXISTS ecs_routing_decisions (
  decision_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ccloud_version              STRING NOT NULL,
  parser_version             STRING NOT NULL,
  raw_output_digest          STRING NOT NULL,
  cluster_id                 STRING NOT NULL,
  cluster_cloud              STRING NOT NULL,
  cluster_state              STRING NOT NULL,
  cluster_regions            STRING[] NOT NULL,
  selected_ecs_region        STRING NOT NULL,
  reason                     STRING NOT NULL,
  observed_at                TIMESTAMPTZ NOT NULL,
  valid_until                TIMESTAMPTZ NOT NULL,
  credential_scope_evidence  STRING NOT NULL,
  namespace_id               STRING,
  request_id                 UUID,
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (valid_until > observed_at),
  CHECK (array_length(cluster_regions, 1) > 0)
);

CREATE INDEX IF NOT EXISTS ecs_routing_decisions_by_cluster_created
  ON ecs_routing_decisions (cluster_id, created_at DESC);

GRANT SELECT ON TABLE ecs_routing_decisions TO cairn_console_ro;
