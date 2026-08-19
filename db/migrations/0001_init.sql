-- 0001_init.sql
-- Identity, causal graph roots, and the distributed claim table.
-- Forward-only. See docs/project/PROJECT.md §11 for the authoritative full schema
-- (later migrations add code_units/code_edges, failure_signatures,
-- remediations, reuse_decisions, probe_runs, contradictions, cost_rates).

CREATE TABLE environments (
  env_fingerprint   STRING PRIMARY KEY,
  image_digest      STRING NOT NULL,
  python_version    STRING NOT NULL,
  deps              JSONB  NOT NULL,       -- sorted resolved dependency set
  torch_threads     INT    NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifacts (
  artifact_id       STRING PRIMARY KEY,     -- content address (sha256 of payload)
  stage             STRING NOT NULL,
  work_key          STRING NOT NULL,
  s3_uri            STRING NOT NULL,
  size_bytes        INT8   NOT NULL,
  env_fingerprint   STRING NOT NULL REFERENCES environments(env_fingerprint),
  produced_by_run   UUID   NOT NULL,
  duration_ms       INT8   NOT NULL,
  vcpu              DECIMAL NOT NULL,
  mem_mib           INT    NOT NULL,
  region            STRING NOT NULL,
  quarantined_at    TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (work_key, created_at DESC),
  INDEX (stage, created_at DESC)
);

CREATE TABLE artifact_inputs (
  artifact_id       STRING NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  input_kind        STRING NOT NULL,        -- code|config|data|upstream|env
  input_ref         STRING NOT NULL,        -- symbol id | config key path | partition id | artifact_id
  input_digest      STRING NOT NULL,
  PRIMARY KEY (artifact_id, input_kind, input_ref)
);

-- Distributed mutual exclusion for expensive stages. See db/claims.py (D2)
-- and docs/project/PROJECT.md §4.2 for the full acquire/heartbeat/takeover protocol this
-- table is designed around: fenced writes, lease expiry, safe takeover.
CREATE TABLE work_claims (
  work_key          STRING PRIMARY KEY,
  stage             STRING NOT NULL,
  state             STRING NOT NULL,        -- CLAIMED|RUNNING|SUCCEEDED|FAILED|ABANDONED
  owner_id          STRING NOT NULL,
  owner_host        STRING NOT NULL,
  owner_region      STRING NOT NULL,
  fence             INT8   NOT NULL DEFAULT 1,
  lease_expires_at  TIMESTAMPTZ NOT NULL,
  cancel_requested  BOOL   NOT NULL DEFAULT false,
  run_id            UUID   NOT NULL,
  artifact_id       STRING,
  claimed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (state, lease_expires_at),
  CHECK (state <> 'SUCCEEDED' OR artifact_id IS NOT NULL)
);

-- ownership_transfers (audit trail for takeovers) lands in 0002_claims.sql
-- alongside db/claims.py; run_fragments (crash-resume checkpoints) lands
-- in 0003_fragments.sql alongside the workload stages that fragment.

CREATE TABLE runs (
  run_id       UUID PRIMARY KEY,
  work_key     STRING NOT NULL,
  stage        STRING NOT NULL,
  state        STRING NOT NULL,             -- RUNNING|SUCCEEDED|FAILED|DISPOSSESSED
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at     TIMESTAMPTZ,
  region       STRING NOT NULL,
  task_arn     STRING,
  INDEX (work_key, started_at DESC)
);
