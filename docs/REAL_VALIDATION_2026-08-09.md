# Real-system validation log — 2026-08-09

Scope: adversarial, end-to-end validation of Cairn's CLI, native Rust TUI,
FastAPI/React console, CockroachDB Cloud integration, and the already-existing
AWS deployment. This log records only observed results. It does not contain
mocked database results, fabricated timings, fabricated identifiers, or
unexecuted claims.

## Guardrails

- AWS commands use the `cairn` profile explicitly.
- No infrastructure provisioning, destructive database statements, or AWS
  resource deletion is part of this session.
- Workload and Bedrock calls are bounded and purposeful.
- Existing resources may be rebuilt or redeployed in place when required.

## Baseline

- Starting branch: `main`, tracking `origin/main`.
- Starting commit: `e74d04250b4332ee00a514a4f6f01f98f3e40245`.
- Starting worktree: clean.
- `.env` exists and defines `CAIRN_DATABASE_URL`, `CAIRN_CLUSTER_NAME`,
  `CAIRN_AWS_REGION`, `CAIRN_APPROVAL_USD`, and `CAIRN_MODE`; values were not
  printed into this log.
- `cargo build --release` in `tui-rs/`: PASS.
- `npm install && npm run build` in `console/frontend/`: PASS; Vite produced
  the local ignored `dist/` bundle.

## Test ledger

Further observations, defects, fixes, live identifiers, and retest results are
appended below as they are obtained.

### Baseline gates

- The first direct pytest invocation intentionally failed its determinism
  guard because `PYTHONHASHSEED` was absent: 170 tests passed and
  `test_current_process_actually_has_it_pinned` was the only failure. The
  harness was corrected to set `PYTHONHASHSEED=0`; the suite then passed
  171/171. This was a validation-harness error, not a Cairn result.
- Ruff check and format check: PASS.
- Mypy strict check: PASS across 53 source files.
- Rust workspace: PASS, 70 tests total with four explicitly ignored live-cloud
  tests.
- Soft-retired TypeScript TUI: typecheck PASS, 53/53 tests PASS.
- React frontend typecheck: PASS.

### Defect 1 — documented `.env` was not loaded

- Reproduction: from the repository root, with the gitignored `.env` present,
  `uv run cairn doctor` reported `CAIRN_DATABASE_URL is not set` for both the
  database and schema checks. The README quickstart says the provision script
  writes `.env` and then invokes Cairn without a separate export step, so the
  documented path was broken.
- Fix: the root Typer callback now loads the nearest `.env` from the invocation
  directory without overriding explicit process environment values. A
  regression test includes a database URL whose query string contains `&`.
- Retest: `cairn doctor` reached the real CockroachDB Cloud cluster and reported
  CockroachDB CCL v26.2.5, eight applied migrations, and the live `fs_sem`
  vector index.

### Defect 2 — doctor falsely reported AWS unavailable on this Windows host

- Reproduction: the isolated boto3 probe exited before producing JSON with
  `OPENSSL_Uplink ... no OPENSSL_Applink`, while an explicit
  `AWS_PROFILE=cairn aws sts get-caller-identity` succeeded against account
  `357199110611`.
- Fix: doctor retains the isolated SDK probe and falls back to the installed,
  isolated AWS CLI when that probe crashes or fails. The output labels the
  successful path.
- Retest: `cairn doctor` reports `PASS aws`, account `357199110611`, via the AWS
  CLI fallback. The database, schema, and vector-index checks remain live and
  green.
