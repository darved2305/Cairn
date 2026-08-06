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

**§6 (`SELECT ... FOR UPDATE`) → `src/cairn/db/claims.py::acquire`.** "Use
when optimistic retries are causing thrashing" is exactly the contended
branch of `acquire`: every loser in a same-key race would otherwise
re-`SELECT` the same stale row and pile up `40001`s against each other.
Locking it before branching on state turns that into a wait instead of
wasted retries.

**Measured**, 2026-08-07, `tests/integration/test_claims.py::test_race_produces_exactly_one_winner_every_time`,
200/200 races, single-node CockroachDB (`scripts/local_cluster.sh`):
**0** `SerializationFailure` retries logged by `db/txn.py` across all 200
races. `FOR UPDATE` isn't just cutting the retry count here, it's
eliminating it — the loser blocks on the row lock instead of racing for
it. This is a local, same-host measurement (both "workers" are threads on
one box with negligible network RTT between them); it's the number to
recheck once D7's real cross-region ECS race introduces actual network
latency between contenders, which is what the ~3.1 → ~0.4 target in
PROJECT.md §10 item 2 was estimating for.

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
