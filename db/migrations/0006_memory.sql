-- 0006_memory.sql
-- D6: negative computational memory (PROJECT.md §4.1) and the
-- contradiction/quarantine mechanism (§6.5). Deliberately does NOT create
-- the vector index here — CREATE VECTOR INDEX is gated by both a cluster
-- setting and actual engine support that varies by plan/build (PLAN.md
-- §13: "target CockroachDB Cloud Standard... cairn doctor detects index
-- availability and falls back to exact brute-force cosine via <=> with no
-- index"). A migration that hard-fails when the index isn't available
-- would make the whole schema undeployable on a cluster that only lacks
-- the index; db/memory.py::ensure_vector_index attempts it separately, at
-- runtime, and reports which path is active instead.

CREATE TABLE failure_signatures (
  signature_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stage             STRING NOT NULL,
  error_class       STRING NOT NULL,
  workload_kind     STRING NOT NULL,
  model_family      STRING,
  model_id          STRING,
  embedding_dim     INT,
  num_labels        INT,
  dataset_rows      INT8,
  max_seq_len       INT,
  batch_size        INT,
  grad_accum        INT,
  precision         STRING,
  optimizer         STRING,
  lr                DECIMAL,
  instance_kind     STRING,
  vcpu              DECIMAL,
  mem_mib           INT,
  accelerator       STRING,
  framework         STRING,
  framework_version STRING,
  error_module      STRING,
  exit_code         INT,
  oom_killed        BOOL NOT NULL DEFAULT false,
  traceback_head    STRING NOT NULL,
  summary_text      STRING NOT NULL,
  embedding         VECTOR(1024) NOT NULL,
  run_id            UUID NOT NULL,
  wasted_ms         INT8 NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (stage, error_class, created_at DESC)
);

CREATE TABLE remediations (
  remediation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signature_id    UUID NOT NULL REFERENCES failure_signatures(signature_id),
  changed_keys    JSONB NOT NULL,           -- [{key, from, to}]
  rationale       STRING NOT NULL,
  verified_run_id UUID,                     -- set when a run using it succeeded
  succeeded       BOOL NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (signature_id, succeeded)
);

CREATE TABLE contradictions (
  contradiction_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id       STRING NOT NULL,
  contradicting_run UUID NOT NULL,
  evidence          STRING NOT NULL,
  quarantined       BOOL NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cost_rates (
  resource_kind  STRING PRIMARY KEY,        -- fargate_vcpu_hour | fargate_gb_hour | ...
  usd            DECIMAL NOT NULL,
  source_note    STRING NOT NULL,           -- e.g. 'AWS published on-demand us-east-1, 2026-08'
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
