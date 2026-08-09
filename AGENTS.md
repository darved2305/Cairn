# Cairn — agent working rules

## What this project is
Cairn is a memory layer for computation. It observes what an expensive command
actually reads, and decides whether a result can be reused, joined, taken over,
repaired, or must run. CockroachDB is the authority plane. S3 is the immutable
byte store. Submission deadline: 2026-08-18 17:00 EDT. Engineering freeze 08-17.

Read `CAIRN_9_DAY_WINNING_PLAN.md` before starting. Appendix A is what the repo
actually is today. Appendix B is the module boundaries and type signatures.
Appendix C is the collector spec. Appendix E is what "done" means each day.

## Non-negotiable correctness rules
1. NEVER invent an API, CLI flag, config key, or SQL feature. If you have not
   read it in this repo or in a doc you fetched this session, you do not know it
   exists. Say "I need to verify X" and stop. A plausible-sounding flag that
   does not exist has already shipped in this repo once.
2. Unknown means run. Any missing evidence, lost trace event, unpinned image, or
   unresolvable resource forces normal execution — never a cache hit.
3. A model may propose reuse. Only deterministic evidence authorizes it. The
   `authorized_by` CHECK in migration 0005 makes model-authorized reuse
   structurally unrepresentable. Do not weaken it.
4. Never write a metric, duration, row count, or digest you did not measure.
   No placeholder numbers in code, docs, UI, or comments.
5. Every fenced write verifies {owner_id, run_id, fence, state} against the live
   claim row inside the same SERIALIZABLE transaction. No exceptions.
6. `db/txn.py::in_txn` retries the WHOLE closure on 40001. Closures must be pure
   functions of their arguments: no S3 calls, no event emission, no side effects.
   Emit events after `in_txn` returns, as `db/claims.py` already does.

## Code quality bar
- Python 3.12. `mypy --strict` and `ruff check` must pass. No `type: ignore`
  without a one-line reason. No new runtime dependencies without asking.
- Match the surrounding comment style: this codebase explains *why* a design is
  the way it is, including the failure it prevents. Do not write "# increment i".
- Frozen dataclasses with `slots=True` for value types. Enforce invariants in
  `__post_init__` so bad states cannot reach the database.
- Reuse `fingerprint/canon.canonical_json` for every digest. Do not add a second
  JSON canonicalizer.
- Tests are not optional and not written last. Every identity function gets a
  frozen golden vector. Every race gets an adversarial test that proves the
  unsafe outcome cannot happen.

## Efficiency bar
- The tracer runs on every wrapped command. Measure its overhead and report it;
  a tool slower than recomputing is worthless.
- Hash file bytes for identity, but memoize per-process on (inode, size, mtime).
  Never let that memo become the identity itself.
- CockroachDB: short transactions, high-entropy keys to spread ranges, batch
  reads, one leaf per acquire/complete transaction. Index by the exact prefix
  the query uses. `EXPLAIN` every new hot query against the live cluster.

## When you finish a task
Report: what you changed, the gate command you ran, its exact output, and what
you could NOT verify. "I believe this works" is not a report.
