-- 0008_console_readonly_role.sql
-- A dedicated SELECT-only SQL role for the console — docs/project/PLAN.md §8 open
-- decision 4 ("Console auth — none for judge mode; write mutations disabled
-- at the IAM/role layer, not just in the UI") and docs/project/PLAN.md D10's security
-- checklist item "read-only console role".
--
-- The gap this closes is real and was previously only a comment: until now
-- the console task read the *same* Secrets Manager secret as the worker
-- tasks, so the only thing standing between a public, unauthenticated demo
-- URL and a write was the discipline of console/queries.py containing
-- nothing but SELECTs. Code discipline is a good first line; it is not a
-- security boundary. This role is.
--
-- Scope of this file, deliberately: it creates the *group* role and its
-- grants only. It does NOT create a login user, because a login user needs a
-- password, and a password does not belong in a committed, forward-only
-- migration. scripts/provision_console_role.py creates the login user, adds
-- it to this role, grants CONNECT on whatever database it is pointed at
-- (which a static SQL file cannot know), prints the connection URL for
-- Secrets Manager, and then proves the result by connecting as that user and
-- asserting that a write is rejected.
--
-- Forward-only, and idempotent: IF NOT EXISTS on the role, and GRANT is
-- itself idempotent.

CREATE ROLE IF NOT EXISTS cairn_console_ro;

-- Exactly the tables the console reads. Listed one per line rather than via
-- ALL TABLES IN SCHEMA so that a future migration adding a table has to make
-- a deliberate decision about whether the public console may read it —
-- ALL TABLES would silently widen this grant on every schema change.
GRANT SELECT ON TABLE environments        TO cairn_console_ro;
GRANT SELECT ON TABLE artifacts           TO cairn_console_ro;
GRANT SELECT ON TABLE artifact_inputs     TO cairn_console_ro;
GRANT SELECT ON TABLE work_claims         TO cairn_console_ro;
GRANT SELECT ON TABLE ownership_transfers TO cairn_console_ro;
GRANT SELECT ON TABLE run_fragments       TO cairn_console_ro;
GRANT SELECT ON TABLE runs                TO cairn_console_ro;
GRANT SELECT ON TABLE code_units          TO cairn_console_ro;
GRANT SELECT ON TABLE code_edges          TO cairn_console_ro;
GRANT SELECT ON TABLE probe_runs          TO cairn_console_ro;
GRANT SELECT ON TABLE reuse_decisions     TO cairn_console_ro;
GRANT SELECT ON TABLE failure_signatures  TO cairn_console_ro;
GRANT SELECT ON TABLE remediations        TO cairn_console_ro;
GRANT SELECT ON TABLE contradictions      TO cairn_console_ro;
GRANT SELECT ON TABLE cost_rates          TO cairn_console_ro;
GRANT SELECT ON TABLE schema_migrations   TO cairn_console_ro;

-- Defensive: make the absence of write privilege explicit and auditable
-- rather than merely implied by never having granted it. A `SHOW GRANTS FOR
-- cairn_console_ro` after this migration shows SELECT and nothing else.
REVOKE INSERT, UPDATE, DELETE ON TABLE
  environments, artifacts, artifact_inputs, work_claims, ownership_transfers,
  run_fragments, runs, code_units, code_edges, probe_runs, reuse_decisions,
  failure_signatures, remediations, contradictions, cost_rates, schema_migrations
FROM cairn_console_ro;
