-- 0010_flight_recorder.sql
-- Flight Recorder schema (CAIRN_9_DAY_WINNING_PLAN.md §20).
-- Each statement is independently idempotent so scripts/migrate.py can be
-- killed between DDL statements and still converge.

ALTER TABLE work_claims ADD COLUMN IF NOT EXISTS derivation_id UUID;

ALTER TABLE work_claims ADD CONSTRAINT IF NOT EXISTS work_claims_success_pointer
  CHECK (
    state <> 'SUCCEEDED'
    OR (artifact_id IS NOT NULL AND derivation_id IS NULL)
    OR (artifact_id IS NULL AND derivation_id IS NOT NULL)
  );

ALTER TABLE work_claims ADD CONSTRAINT IF NOT EXISTS work_claims_state_membership
  CHECK (state IN (
    'CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED', 'INVALIDATED'
  ));

CREATE TABLE IF NOT EXISTS namespaces (
  namespace_id             STRING PRIMARY KEY,
  display_name             STRING NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS namespace_principals (
  namespace_id             STRING NOT NULL REFERENCES namespaces(namespace_id),
  oidc_issuer              STRING NOT NULL,
  oidc_subject             STRING NOT NULL,
  role                     STRING NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace_id, oidc_issuer, oidc_subject),
  CHECK (role IN ('READER', 'WRITER', 'ADMIN')),
  INDEX principal_to_namespace (oidc_issuer, oidc_subject)
    STORING (role)
);

CREATE TABLE IF NOT EXISTS execution_specs (
  spec_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace_id            STRING NOT NULL REFERENCES namespaces(namespace_id),
  compatibility_key       STRING NOT NULL,
  spec_digest             STRING NOT NULL,
  argv                    JSONB NOT NULL,
  cwd_rel                 STRING NOT NULL,
  output_contract         JSONB NOT NULL,
  platform_contract       JSONB NOT NULL,
  purity_policy           JSONB NOT NULL,
  coverage_profile_digest STRING NOT NULL,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (namespace_id, spec_digest),
  UNIQUE (spec_id, namespace_id),
  INDEX spec_selector (namespace_id, compatibility_key, created_at DESC)
    STORING (spec_digest, coverage_profile_digest)
);

CREATE TABLE IF NOT EXISTS trace_contents (
  trace_digest             STRING PRIMARY KEY,
  coverage_profile_digest  STRING NOT NULL,
  input_resource_set_digest STRING NOT NULL,
  output_evidence_digest   STRING NOT NULL,
  coverage_state           STRING NOT NULL,
  incomplete_reasons       JSONB NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (coverage_state IN (
    'COMPLETE_SUPPORTED', 'COMPLETE_DECLARED', 'SHADOW_UNQUALIFIED',
    'INCOMPLETE_NETWORK', 'INCOMPLETE_TRACE_LOSS',
    'INCOMPLETE_WRITE', 'INCOMPLETE_PLATFORM',
    'INCOMPLETE_INPUT_RACE', 'NONDETERMINISTIC'
  ))
);

CREATE TABLE IF NOT EXISTS trace_resources (
  trace_digest            STRING NOT NULL
                            REFERENCES trace_contents(trace_digest) ON DELETE CASCADE,
  resource_kind           STRING NOT NULL,
  resource_ref            STRING NOT NULL,
  access_mode             STRING NOT NULL,
  "exists"                BOOL NOT NULL,
  version_digest          STRING NOT NULL,
  resolver                STRING NOT NULL,
  observation_source      STRING NOT NULL,
  metadata                JSONB NOT NULL,
  PRIMARY KEY (trace_digest, resource_kind, resource_ref, access_mode),
  CHECK (access_mode IN ('read', 'execute', 'enumerate', 'negative', 'write'))
);

CREATE TABLE IF NOT EXISTS trace_observations (
  observation_id          UUID PRIMARY KEY,
  namespace_id            STRING NOT NULL,
  spec_id                 UUID NOT NULL,
  trace_digest            STRING NOT NULL REFERENCES trace_contents(trace_digest),
  run_id                  UUID NOT NULL REFERENCES runs(run_id),
  semantic_work_key       STRING NOT NULL,
  lifecycle_state         STRING NOT NULL,
  supersedes_observation_id UUID,
  validated_by_run_id     UUID REFERENCES runs(run_id),
  observed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, spec_id),
  UNIQUE (observation_id, namespace_id),
  FOREIGN KEY (spec_id, namespace_id)
    REFERENCES execution_specs (spec_id, namespace_id),
  FOREIGN KEY (supersedes_observation_id, namespace_id)
    REFERENCES trace_observations (observation_id, namespace_id),
  INDEX observation_selector (spec_id, lifecycle_state, observed_at DESC)
    STORING (trace_digest, semantic_work_key),
  CHECK (lifecycle_state IN (
    'CANDIDATE', 'VALIDATED', 'SUPERSEDED', 'INVALIDATED', 'INCOMPLETE'
  )),
  CHECK (lifecycle_state <> 'VALIDATED' OR validated_by_run_id IS NOT NULL)
);

ALTER TABLE failure_signatures ADD CONSTRAINT IF NOT EXISTS failure_signature_stage_identity
  UNIQUE (signature_id, stage, error_class);

CREATE TABLE IF NOT EXISTS failure_embedding_revisions (
  embedding_revision_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signature_id             UUID NOT NULL,
  stage                    STRING NOT NULL,
  error_class              STRING NOT NULL,
  embedding_space_id       STRING NOT NULL,
  provider_id              STRING NOT NULL,
  model_revision           STRING NOT NULL,
  model_weights_digest     STRING NOT NULL,
  source_text_digest       STRING NOT NULL,
  dimension                INT8 NOT NULL,
  normalized               BOOL NOT NULL,
  embedding                VECTOR(384) NOT NULL,
  state                    STRING NOT NULL DEFAULT 'ACTIVE',
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (signature_id, embedding_space_id),
  FOREIGN KEY (signature_id, stage, error_class)
    REFERENCES failure_signatures (signature_id, stage, error_class),
  CHECK (dimension = 384),
  CHECK (normalized),
  CHECK (state IN ('ACTIVE', 'RETIRED')),
  INDEX failure_embeddings_by_space
    (embedding_space_id, stage, error_class, state)
    STORING (model_revision, source_text_digest)
);

-- Vector index availability varies by plan/build (same reason 0006 omits
-- fs_sem). Enable the feature when permitted; migrate.py treats failure here
-- as optional so a Standard/local cluster without the setting still converges.
SET CLUSTER SETTING feature.vector_index.enabled = true;

CREATE VECTOR INDEX IF NOT EXISTS fs_sem_v2 ON failure_embedding_revisions
  (embedding_space_id, stage, error_class, embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS content_blobs (
  blob_digest              STRING PRIMARY KEY,
  bucket                   STRING NOT NULL,
  object_key               STRING NOT NULL,
  version_id               STRING NOT NULL,
  checksum_sha256          STRING NOT NULL,
  size_bytes               INT8 NOT NULL,
  canonicalization_version STRING NOT NULL,
  integrity_state          STRING NOT NULL DEFAULT 'VALID',
  quarantined_at           TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (bucket, object_key, version_id),
  CHECK (size_bytes >= 0),
  CHECK (integrity_state IN ('VALID', 'INVALID')),
  CHECK ((integrity_state = 'INVALID') = (quarantined_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS work_heads (
  namespace_id             STRING NOT NULL REFERENCES namespaces(namespace_id),
  semantic_work_key        STRING NOT NULL,
  current_generation       INT8 NOT NULL,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace_id, semantic_work_key),
  CHECK (current_generation > 0)
);

CREATE TABLE IF NOT EXISTS work_generations (
  namespace_id             STRING NOT NULL,
  semantic_work_key        STRING NOT NULL,
  generation               INT8 NOT NULL,
  claim_key                STRING NOT NULL UNIQUE REFERENCES work_claims(work_key),
  lifecycle_state          STRING NOT NULL,
  terminal_reason          STRING,
  current_derivation_id    UUID,
  publication_operation_id UUID UNIQUE,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace_id, semantic_work_key, generation),
  CHECK (generation > 0),
  CHECK (lifecycle_state IN ('PENDING', 'PUBLISHED', 'INVALIDATED', 'SUPERSEDED')),
  CHECK (lifecycle_state <> 'PUBLISHED' OR current_derivation_id IS NOT NULL)
);

ALTER TABLE work_heads ADD CONSTRAINT IF NOT EXISTS head_generation_fk
  FOREIGN KEY (namespace_id, semantic_work_key, current_generation)
  REFERENCES work_generations (namespace_id, semantic_work_key, generation);

CREATE TABLE IF NOT EXISTS derivations (
  derivation_id            UUID PRIMARY KEY,
  namespace_id             STRING NOT NULL,
  semantic_work_key        STRING NOT NULL,
  generation               INT8 NOT NULL,
  blob_digest              STRING NOT NULL REFERENCES content_blobs(blob_digest),
  observation_id           UUID,
  produced_by_run          UUID NOT NULL REFERENCES runs(run_id),
  committed_fence          INT8 NOT NULL,
  rule_id                  STRING,
  rule_revision            INT8,
  state                    STRING NOT NULL,
  quarantined_at           TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (namespace_id, semantic_work_key, generation),
  UNIQUE (derivation_id, namespace_id),
  UNIQUE (derivation_id, namespace_id, semantic_work_key, generation),
  FOREIGN KEY (namespace_id, semantic_work_key, generation)
    REFERENCES work_generations (namespace_id, semantic_work_key, generation),
  FOREIGN KEY (observation_id, namespace_id)
    REFERENCES trace_observations (observation_id, namespace_id),
  INDEX derivations_by_blob (blob_digest)
    STORING (namespace_id, semantic_work_key, generation, state),
  INDEX derivations_by_rule (rule_id, rule_revision)
    STORING (namespace_id, semantic_work_key, generation, state),
  CHECK (state IN ('PUBLISHED', 'QUARANTINED')),
  CHECK ((state = 'QUARANTINED') = (quarantined_at IS NOT NULL)),
  CHECK (committed_fence > 0),
  CHECK ((rule_id IS NULL) = (rule_revision IS NULL))
);

CREATE TABLE IF NOT EXISTS work_subscribers (
  namespace_id             STRING NOT NULL,
  semantic_work_key        STRING NOT NULL,
  generation               INT8 NOT NULL,
  subscriber_id            UUID NOT NULL,
  request_id               UUID NOT NULL,
  run_id                   UUID NOT NULL REFERENCES runs(run_id),
  joined_fence             INT8 NOT NULL,
  state                    STRING NOT NULL,
  lease_expires_at         TIMESTAMPTZ NOT NULL,
  joined_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  detached_at              TIMESTAMPTZ,
  PRIMARY KEY (namespace_id, semantic_work_key, generation, subscriber_id),
  UNIQUE (namespace_id, semantic_work_key, generation, request_id),
  FOREIGN KEY (namespace_id, semantic_work_key, generation)
    REFERENCES work_generations (namespace_id, semantic_work_key, generation)
    ON DELETE CASCADE,
  CHECK (state IN ('LIVE', 'DETACHED', 'COMPLETED', 'FAILED', 'EXPIRED')),
  CHECK (joined_fence > 0),
  INDEX subscriber_reaper (state, lease_expires_at),
  INDEX subscribers_by_run (run_id)
    STORING (state)
);

CREATE TABLE IF NOT EXISTS fragment_commits (
  namespace_id             STRING NOT NULL,
  semantic_work_key        STRING NOT NULL,
  generation               INT8 NOT NULL,
  microchunk_key           STRING NOT NULL,
  input_slice_digest       STRING NOT NULL,
  blob_digest              STRING NOT NULL REFERENCES content_blobs(blob_digest),
  committed_by_run         UUID NOT NULL REFERENCES runs(run_id),
  committed_fence          INT8 NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (namespace_id, semantic_work_key, generation, microchunk_key),
  FOREIGN KEY (namespace_id, semantic_work_key, generation)
    REFERENCES work_generations (namespace_id, semantic_work_key, generation)
    ON DELETE CASCADE,
  CHECK (committed_fence > 0)
);

CREATE TABLE IF NOT EXISTS composite_derivations (
  parent_derivation_id     UUID PRIMARY KEY REFERENCES derivations(derivation_id) ON DELETE CASCADE,
  adapter_id               STRING NOT NULL,
  partitioner_digest       STRING NOT NULL,
  reducer_digest           STRING NOT NULL,
  verifier_digest          STRING NOT NULL,
  merkle_root_digest       STRING NOT NULL,
  leaf_count               INT8 NOT NULL,
  probe_run_id             UUID REFERENCES probe_runs(probe_run_id),
  output_metadata          JSONB NOT NULL,
  CHECK (leaf_count > 0)
);

CREATE TABLE IF NOT EXISTS derivation_fragments (
  namespace_id             STRING NOT NULL,
  parent_derivation_id     UUID NOT NULL,
  partition_key            STRING NOT NULL,
  ordinal                  INT8 NOT NULL,
  child_derivation_id      UUID NOT NULL,
  input_slice_digest       STRING NOT NULL,
  PRIMARY KEY (parent_derivation_id, partition_key),
  UNIQUE (parent_derivation_id, ordinal),
  FOREIGN KEY (parent_derivation_id, namespace_id)
    REFERENCES derivations (derivation_id, namespace_id) ON DELETE CASCADE,
  FOREIGN KEY (child_derivation_id, namespace_id)
    REFERENCES derivations (derivation_id, namespace_id),
  INDEX parents_by_child (child_derivation_id)
    STORING (namespace_id),
  CHECK (ordinal >= 0)
);

CREATE TABLE IF NOT EXISTS reuse_rule_revisions (
  rule_id                  STRING NOT NULL,
  revision                 INT8 NOT NULL,
  state                    STRING NOT NULL,
  required_authority       STRING NOT NULL,
  contradiction_id         UUID REFERENCES contradictions(contradiction_id),
  reason                   STRING NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (rule_id, revision),
  CHECK (state IN ('ACTIVE', 'TIGHTENED', 'SUPERSEDED', 'DISABLED')),
  CHECK (required_authority IN ('identity', 'structural', 'probe', 'recompute'))
);

CREATE TABLE IF NOT EXISTS reuse_rule_heads (
  rule_id                  STRING PRIMARY KEY,
  current_revision         INT8 NOT NULL,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (rule_id, current_revision)
    REFERENCES reuse_rule_revisions (rule_id, revision)
);

ALTER TABLE derivations ADD CONSTRAINT IF NOT EXISTS derivation_rule_revision_fk
  FOREIGN KEY (rule_id, rule_revision)
  REFERENCES reuse_rule_revisions (rule_id, revision);

ALTER TABLE work_generations ADD CONSTRAINT IF NOT EXISTS generation_derivation_fk
  FOREIGN KEY (current_derivation_id, namespace_id, semantic_work_key, generation)
  REFERENCES derivations (derivation_id, namespace_id, semantic_work_key, generation);

ALTER TABLE work_claims ADD CONSTRAINT IF NOT EXISTS claim_derivation_fk
  FOREIGN KEY (derivation_id) REFERENCES derivations(derivation_id);

ALTER TABLE reuse_decisions ADD COLUMN IF NOT EXISTS observation_id UUID;
ALTER TABLE reuse_decisions ADD COLUMN IF NOT EXISTS derivation_id UUID;
ALTER TABLE reuse_decisions ADD COLUMN IF NOT EXISTS rule_id STRING;
ALTER TABLE reuse_decisions ADD COLUMN IF NOT EXISTS rule_revision INT8;

ALTER TABLE reuse_decisions ADD CONSTRAINT IF NOT EXISTS decision_observation_fk
  FOREIGN KEY (observation_id) REFERENCES trace_observations(observation_id);

ALTER TABLE reuse_decisions ADD CONSTRAINT IF NOT EXISTS decision_derivation_fk
  FOREIGN KEY (derivation_id) REFERENCES derivations(derivation_id);

ALTER TABLE reuse_decisions ADD CONSTRAINT IF NOT EXISTS decision_rule_revision_fk
  FOREIGN KEY (rule_id, rule_revision)
  REFERENCES reuse_rule_revisions (rule_id, revision);

ALTER TABLE reuse_decisions ADD CONSTRAINT IF NOT EXISTS decision_rule_pair_check
  CHECK ((rule_id IS NULL) = (rule_revision IS NULL));
