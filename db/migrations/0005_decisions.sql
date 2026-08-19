-- 0005_decisions.sql
-- D5: the decision ledger and probe evidence. docs/project/PROJECT.md §3.1's safety
-- rule — "The model may propose reuse. Deterministic evidence must
-- authorize it." — is enforced here structurally, not by convention: the
-- CHECK constraint below makes verdict='reuse' with authorized_by='model'
-- unrepresentable. There is no enum value 'model' in authorized_by at all.

CREATE TABLE probe_runs (
  probe_run_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  probe_type      STRING NOT NULL,          -- P1..P6
  artifact_id     STRING,
  sample_spec     STRING NOT NULL,          -- exact deterministic selection rule
  population_size INT8 NOT NULL,
  sample_size     INT8 NOT NULL,
  tolerance       STRING NOT NULL,          -- 'bitwise' in conservative mode
  runtime_ms      INT8 NOT NULL,
  passed          BOOL NOT NULL,
  evidence_digest STRING NOT NULL,
  detail          STRING NOT NULL DEFAULT '',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (artifact_id, created_at DESC)
);

CREATE TABLE reuse_decisions (
  decision_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  work_key               STRING NOT NULL,
  stage                  STRING NOT NULL,
  action                 STRING NOT NULL,   -- the nine actions, docs/project/PROJECT.md §6.4
  verdict                STRING NOT NULL,   -- reuse|recompute|refused|subscribed|resumed
  change_class           STRING,            -- comment_only|logging_only|...
  proposed_by            STRING NOT NULL,   -- rule|model
  model_id               STRING,
  authorized_by          STRING,            -- probe|structural|identity  (NEVER 'model')
  probe_run_id           UUID REFERENCES probe_runs(probe_run_id),
  candidate_artifact_id  STRING,
  latency_ms             INT8 NOT NULL,
  explanation            STRING NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (created_at DESC),
  INDEX (work_key, created_at DESC),
  CHECK (proposed_by IN ('rule', 'model')),
  CHECK (authorized_by IS NULL OR authorized_by IN ('probe', 'structural', 'identity')),
  -- NOT a plain `verdict <> 'reuse' OR authorized_by IN (...)`: SQL's
  -- three-valued logic makes `authorized_by IN (...)` evaluate to NULL
  -- (not FALSE) when authorized_by IS NULL, and a CHECK only rejects a
  -- row when the expression is FALSE — NULL passes silently. Without the
  -- explicit `IS NOT NULL`, a verdict='reuse' row with no authorized_by
  -- at all would slip through unauthorized, which is exactly the case
  -- docs/project/PROJECT.md §3.1 exists to make impossible.
  CHECK (
    verdict <> 'reuse'
    OR (authorized_by IS NOT NULL AND authorized_by IN ('probe', 'structural', 'identity'))
  )
);
