# Security model

What Cairn is trusted to do, what it is not, and where each boundary is
enforced. Every statement here names the file that enforces it.

---

## 1. Reuse authority is fail-closed by construction

The one rule the whole system is built around:

> A model may propose reuse. Only deterministic evidence authorizes it.

`reuse_decisions.authorized_by` accepts exactly three values — `identity`,
`structural`, `probe` — and `db/migrations/0005_decisions.sql` carries a
`CHECK` constraint over them. There is no enum value `model`, so a
model-authorized reuse is not representable in the schema. `ReuseDecision`
(`src/cairn/db/decisions.py`) enforces the same rule in Python before the
round-trip, so the constraint is the last line of defence and not the first.
`tests/integration/test_decisions.py` proves the constraint still rejects a raw
`INSERT` that tries to bypass the Python layer.

The same principle governs the trace layer. `Authority` in
`src/cairn/flight/types.py` has three members and none of them is a model.

**Unknown means run.** Any missing evidence — a lost trace event, an unpinned
image, an unresolvable resource, a hidden or new resource, socket activity
without a stable adapter — forces normal execution. There is no code path where
absence of evidence produces a cache hit.

## 2. Ownership is enforced in the database, never in a process

Cairn holds no in-memory locks. Every claim decision is one `SERIALIZABLE`
transaction (`src/cairn/db/txn.py`, retried whole on `40001`).

- `work_claims` carries `{owner_id, run_id, fence, state, lease_expires_at}`.
- Takeover of an expired or terminal claim increments `fence` and writes an
  `ownership_transfers` row in the same transaction
  (`src/cairn/db/flight.py::_acquire_claim_on_cursor`).
- Every subsequent write re-verifies `{owner_id, run_id, fence, state}` against
  the live claim row *inside the same transaction*
  (`publish_derivation`, `commit_microchunk`, `src/cairn/db/fragments.py`).

A dispossessed owner that comes back therefore updates zero rows and gets
`PublishOutcome.REJECTED_FENCE`. It cannot corrupt the head. A lease alone
would not give this property, because a lease can expire while the old owner is
still alive and still holding a completed result in memory.

## 3. Namespace boundary

`namespaces` and `namespace_principals` (`db/migrations/0010_flight_recorder.sql`)
scope every execution spec, observation, generation, and derivation. A
principal is `(oidc_issuer, oidc_subject)` with a role of `READER`, `WRITER`,
or `ADMIN`.

**Current status, stated plainly:** the schema exists and every Flight Recorder
query is namespace-scoped, but the OIDC → `namespace_principals` short-lived
token exchange is **not implemented** — there is no exchange service in `src/`.
The `.github/actions/cairn` composite Action carries this as an explicit cut in
its example workflow. Until that lands, namespace separation is a data-model
boundary, not an authentication boundary, and a caller holding
`CAIRN_DATABASE_URL` can address any namespace.

## 4. Console read path

The public console is unauthenticated by design (judge mode). Two things stand
between it and a write:

1. Every function in `src/cairn/console/queries.py` is a plain `SELECT`, and
   every read route is backed by exactly one of them.
2. `db/migrations/0008_console_readonly_role.sql` creates `cairn_console_ro`
   with `GRANT SELECT` on an explicitly enumerated table list — not
   `ALL TABLES IN SCHEMA`, so a future migration adding a table has to make a
   deliberate decision about whether the public console may read it.

Code discipline is (1). The security boundary is (2).
`scripts/provision_console_role.py` creates the login user and verifies the
result by reconnecting as that user and asserting a write is rejected. Until it
is run against a deployment, the console shares the workers' credential and only
(1) applies. `infra/ecs.tf` carries the wiring block for giving the console its
own Secrets Manager secret.

The Memory Inspector's SQL surface is guarded separately by
`src/cairn/console/sqltools.py::guard_sql`: statement-kind checks, a query
timeout, a row limit, a response byte cap, and a `crdb_internal` refusal.

## 5. Supervised execution is observation, not sandboxing

`cairn scout` supervises the child under `strace -f` with pinned flags
(`src/cairn/trace/collector.py`). The kernel process-tree collector is the
coverage boundary. The Python `sitecustomize` audit-hook companion
(`src/cairn/trace/companion.py`) **may only add resource rows and refine
`ref` labels — it can never upgrade `coverage_state`**, because audit hooks are
not a sandbox boundary.

Consequences you should assume:

- Cairn does not confine the command it runs. Run it on commands you already
  trust to run on that machine.
- `--network deny` is checked, not enforced. Socket activity without a stable
  adapter yields `INCOMPLETE_NETWORK` and a non-reusable result.
- Arbitrary opaque `exec` is frozen at `SHADOW_UNQUALIFIED` coverage.
  Observation alone never authorizes generic verified reuse; reuse requires a
  named contract.
- Native Windows runs have no portable identity and report
  `INCOMPLETE_PLATFORM`.

Raw strace logs are redacted and removed after normalization unless
`--keep-raw-trace` is passed.

## 6. Content integrity

Blobs are published under `cas/sha256/<digest>` with a conditional
`If-None-Match: *` put (`src/cairn/storage/cas.py`), so first-writer-wins is
atomic. A `412` forces an exact-version fetch and rehash, so a bucket-integrity
failure cannot be mistaken for a benign digest collision.

`BlobUnavailable` (timeout, 5xx) must never mark a blob `INVALID`;
`BlobIntegrityError` (digest mismatch) is the only path that may. `cairn receipt
--verify` re-fetches every named blob from S3 and rehashes it rather than
trusting the stored digest.

## 7. Contradiction and quarantine

If later evidence contradicts an earlier reuse, `src/cairn/db/contradictions.py`
writes a `contradictions` row, sets `artifacts.quarantined_at`, and transitively
quarantines every artifact downstream of it via
`artifact_inputs.input_kind = 'upstream'`. Quarantine is one-way; the only exit
is `cairn unquarantine <id> --reason "<text>"`, which is recorded.

`contradict_and_tighten` (`src/cairn/db/flight.py`) additionally advances the
reuse rule head to a tightened revision, and `publish_derivation` rejects a
publish carrying a stale rule revision with `STALE_AUTHORITY`.

## 8. Infrastructure posture

From `infra/`:

- S3: public access blocked, server-side encryption, versioning on.
- ALB reachable only from CloudFront; workers are egress-only with no inbound.
- IAM roles are per-task and least-privilege: S3 prefix-scoped,
  `bedrock:InvokeModel` on two model ARNs only.
- Database URLs are delivered through Secrets Manager, not task-definition
  environment variables.
- `.terraform.lock.hcl` is committed on purpose so provider builds are pinned;
  state, plans, and `*.tfvars` are not.

## 9. Not in scope

Cairn is not a sandbox, a policy engine, a compliance tool, or a supply-chain
attestation system. It does not verify that the command you gave it is safe. It
records what that command did, and refuses to reuse a result whose evidence does
not support reuse.
