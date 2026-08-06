# CockroachDB Skills Usage

Installed via `npx skills add cockroachlabs/cockroachdb-skills --copy` into
`.agents/skills/` (34 skills, Apache-2.0). `--copy` instead of the default
symlink install because OneDrive-synced directories on Windows silently
drop the symlinks the tool tries to create for per-agent shortcuts; `--copy`
writes real files into the committed, agent-agnostic location PLAN.md
specifies, so this repo's provenance doesn't depend on that workaround.

This file is updated as each skill causes a real, file-level change —
"we installed it" is not meaningful use. See PROJECT.md §10 for the
running summary; this doc carries the detail.

## `designing-application-transactions`

`.agents/skills/designing-application-transactions/SKILL.md`

**§3 (client-side retry loop) → `src/cairn/db/txn.py`.** The skill's
retry example loops on `SerializationFailure` around a caller-supplied
closure, not around individual statements, with the explicit warning that
generic savepoints do not work as a CockroachDB retry mechanism and that
the whole unit of work must be replayed on `40001`. `in_txn()` in
`db/txn.py` implements exactly that shape: `fn` is the whole transaction
body, retried from scratch, never resumed. The module docstring states
the pure-function requirement this implies — no S3/Bedrock calls inside
`fn` — because a duplicated side effect on retry is worse than the wasted
compute Cairn exists to prevent.

**§13 (connection pool sizing) → `src/cairn/db/pool.py`.** The skill's
HikariCP guidance (`maximumPoolSize = (vCPUs * 4) / pool_instances`,
`maxLifetime` ~30 min with jitter) is what `get_pool()`'s
`min_size=2, max_size=10, max_lifetime=1800` is sized against: a Fargate
task at 2 vCPU doesn't need — and under contention on `work_claims`,
actively should not have — a large pool racing itself for the same rows.

**Pending (lands with `db/claims.py`, D2).** §6's `SELECT ... FOR UPDATE`
guidance ("use when optimistic retries are causing thrashing") is the
basis for locking the contended row in the claim-acquire path before
branching on its state, instead of a plain re-`SELECT`. PROJECT.md §10
item 2 records the retry-count delta once the race test exists to measure
it (target: cut mean `40001` retries per contended claim from ~3.1 to
~0.4, per the design's stated benchmark).

## Pending skills (referenced by day, not yet applied)

- **Vector index prefix columns** (D6) — `failure_signatures` gets
  `(stage, error_class, embedding vector_cosine_ops)` rather than
  `(embedding)` alone, because CockroachDB only accelerates filtered
  vector search when filters hit prefix columns.
- **Batch-insert avoidance for `VECTOR` columns** (D6) — failure
  signatures are written one row at a time.
- **`provisioning-cluster-for-production`** — informs
  `scripts/provision_cluster.sh`'s plan/region choices (D1, this file's
  neighbor) once the cluster is actually provisioned against a live
  account.
