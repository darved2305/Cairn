-- 0004_causal_graph.sql
-- Static code provenance for D4 work-key derivation. Artifact nodes and
-- typed artifact_inputs landed in 0001; these tables make the code side of
-- the causal graph queryable and auditable by commit.

CREATE TABLE code_units (
  unit_id           STRING PRIMARY KEY,     -- module:qualname
  module            STRING NOT NULL,
  qualname          STRING NOT NULL,
  ast_digest        STRING NOT NULL,
  docstring_digest  STRING NOT NULL,        -- recorded, but excluded from ast_digest
  is_private        BOOL   NOT NULL,
  commit_sha        STRING NOT NULL,
  INDEX code_units_by_commit (commit_sha, module)
    STORING (qualname, ast_digest, docstring_digest, is_private)
);

CREATE TABLE code_edges (
  commit_sha  STRING NOT NULL,
  src_unit    STRING NOT NULL,
  dst_unit    STRING NOT NULL,
  edge_kind   STRING NOT NULL,
  PRIMARY KEY (commit_sha, src_unit, dst_unit, edge_kind),
  CHECK (edge_kind IN ('calls', 'imports', 'reads_global'))
);
