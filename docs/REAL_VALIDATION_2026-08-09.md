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

### Real CockroachDB integration suite

- Read-only CLI integration: 8/8 PASS.
- Console API integration: 8/8 PASS after the product-data filtering fix below.
- Graph: 3/3; memory: 5/5; CLI claim demo: 3/3; decisions: 8/8;
  reaper: 5/5; agent remediation: 2/2; D5: 2/2; D6: 1/1; events:
  3/3; P3: 2/2; determinism: 1/1; non-race claims: 4/4.
- The dedicated 200-race SERIALIZABLE contention test ran against the live
  cluster for 360.57 seconds and passed 200/200 with exactly one winner each.
- Two S3 integration cases skipped because their pytest fixtures intentionally
  require an external bucket setting. Their behavior is covered by the bounded
  real ECS workload runs later in this ledger; they are not counted as passes.

### Defect 3 — Windows native SDK crashes took down live commands

- Reproduction: local `/api/memory/search` and Memory Inspector requests could
  abort the whole uvicorn process with `OPENSSL_Uplink: no OPENSSL_Applink` when
  botocore and psycopg-binary loaded incompatible native OpenSSL runtimes.
- Fix: on Windows only, real Titan calls and the real Inspector loop run in a
  bounded child process. Linux/ECS and injected test clients retain the direct
  path. A child native abort, timeout, malformed response, or AWS denial becomes
  the existing typed 503/error contract instead of killing the caller.
- Retest: Titan returned a clean 503 while uvicorn remained healthy; Inspector
  returned a clean 503 while uvicorn remained healthy. The deployed Linux
  service reached Bedrock normally and returned the account's real model-access
  denial for both Titan and Claude Sonnet.

### Defect 4 — validation fixtures displaced the product story

- Reproduction against the deployed console: Pipeline showed `s3://test/...`
  integration artifacts as the latest work, Claim Theatre was flooded by 200
  `race_test` claims, and Savings reported a negative value as "seconds saved"
  with a `$-...` formula.
- Fix: public pipeline, ledger, claims, demo, and savings queries now select
  canonical 64-hex Cairn work keys and exclude the documented test URI prefixes
  and `race_test` claims. Audit access remains available through
  `include_validation=true`. Negative savings are honestly labelled "seconds
  added" / "slower" and use absolute display magnitudes while the API retains
  its signed numeric values.
- All 12 changed SQL shapes were run through `EXPLAIN` against the live
  CockroachDB cluster. The local endpoints then returned one real product
  decision, no validation claims, and zero measured reuse savings rather than
  fabricated or fixture-derived savings.

### ECS cross-region race — deployment defects found by running it

- Attempt 1 used real task ARNs
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/a242079218d54104aced8ebd692956f5`
  and
  `arn:aws:ecs:us-west-2:357199110611:task/cairn-us-west-2/ffb5d0d5243f4a6bbec27d18fa7842cb`.
  Both failed before claiming because the existing worker secret embedded the
  local Windows CA path `C:/Users/mites/.aws/ca-bundle-with-avast.pem`.
- The existing secret was rotated in place—no resource was created—to use
  `sslrootcert=system`; its new version is
  `abbfd1ec-9146-4da0-8f15-b9d5835a2546`.
- Attempt 2 used real task ARNs
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/3d42063e88bd46679bc3cb029adb84c2`
  and
  `arn:aws:ecs:us-west-2:357199110611:task/cairn-us-west-2/01384eb8873547ccafb99af7e3cdfda2`.
  Both advanced to TLS verification, then exposed that the pinned deployed
  image could not build CockroachDB Cloud's certificate chain. The current
  Dockerfile already contains the committed intermediate-chain repair; the
  current image rebuild/redeploy and exact race retest follow below.

### Current local gates before image build

- Python unit/property suite: 178/178 PASS.
- Ruff: PASS. Strict mypy over the 53 source files: PASS.
- React typecheck and production build: PASS.

### Container TLS and deployed revision 4

- The first rebuilt image still failed TLS. Inspection showed
  `update-ca-certificates` had skipped the committed three-certificate file:
  Debian requires one certificate per `.crt`. The Dockerfile now splits YE2,
  Root YE, and ISRG Root X2 before updating the store; its build log reported
  three certificates added.
- psycopg's bundled libpq/OpenSSL did not honor `sslrootcert=system` on this
  image. With the explicit URL-encoded
  `/etc/ssl/certs/ca-certificates.crt`, the same image connected and returned
  CockroachDB CCL v26.2.5. Both existing DB secrets were rotated in place to
  that path. Terraform variables now reject workstation paths and `system` so
  the defect cannot silently recur.
- Image `44cc380` was pushed to the existing ECR repository under its immutable
  tag and `latest`, digest
  `sha256:166753c17fd61fa8aeafd1f1a143e278b076e5ddf057b6a3dd13ad974f64b618`.
- Existing task families only were revised: east worker revision 4, west
  worker revision 4, console revision 4. The existing console service reached
  ECS `COMPLETED` rollout state with desired/running 1/1.

### Real cross-region race — PASS

- Final work key: `race-ecs-884f679e43e64b7989e32373cb57b335`.
- East task:
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/59980ddf07204e0cb48f6f0cef1995cc`.
- West task:
  `arn:aws:ecs:us-west-2:357199110611:task/cairn-us-west-2/a46d738b0d0a42aa916e1ae27c50f967`.
- Both tasks exited 0. The real CockroachDB row ended `SUCCEEDED`, winner
  `ip-172-31-6-93.us-west-2.compute.internal-1`, region `us-west-2`, fence 1,
  artifact `c9d0e8a33f57d11c036699f2037d9ef7f8a463f4a6334e5e179632ab9fa944d7`.

### Deployed endpoint matrix on revision 4

- Frontend document, its hashed JavaScript asset, and its hashed CSS asset:
  all HTTP 200.
- Health, pipeline, default/product decisions, decision detail, validation
  decisions, default/product claims, validation claims, Inspector status,
  savings, demo reset, demo run, and demo state: all HTTP 200.
- Titan search: HTTP 503 with the real Bedrock `ValidationException: Operation
  not allowed`; no guessed results. Inspector question: HTTP 503 with the real
  Claude Sonnet account permission error; no fabricated answer.
- Demo run declared `mode=replay`, `writes_to_database=false`, and
  `launches_compute=false`, matching its actual behavior.

### Defect 5 — graph persistence used thousands of cloud round trips

- Reproduction: `cairn plan --persist-graph` exceeded two minutes. Live SQL
  triage found one Cairn SERIALIZABLE transaction open for 2m47s with 658
  statements, zero retries; it was still processing 480 units plus 1,801
  edges one statement at a time.
- The test process was stopped, rolling back its transaction. The two insert
  shapes now use psycopg 3 pipelined `executemany`; DELETE and both INSERT
  shapes passed live `EXPLAIN`.
- Exact retest persisted 480 code units and 1,801 edges at commit `44cc380`
  in 22.247 seconds and exited 0.

### Real workload deployment defects and remediation

- First env task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/da9612854b7442ac8d8b8d7a9af0ba84`
  reached a real PutObject and failed because the worker role omitted `env/*`.
  The existing inline role policy and Terraform source were updated; IAM
  simulation now returns `allowed` for env PutObject and GetObject.
- Exact env retest task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/b9f4a46a9fe94efd8ce3cc82be36de46`
  exited 0 with RECOMPUTE. Artifact
  `7e80c7ee71ae4e0773c8cc08718457113d9c28ef4fc5aa0536b4729095d6342e`
  was observed by `head-object` at its real S3 key, 1,652 bytes.
- Dataset task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/9b2d055366304535b7ab07a6bedcd38f`
  then failed with a real NoSuchKey because `cairn.yaml` still named the
  explicitly provisional `unvendored-20news-4cat-v1` snapshot.
- That unrelated missing input also exposed a logic bug: the loop blindly
  quarantined the last reused upstream for every downstream exception, even
  though the env object's S3 HEAD succeeded. Quarantine is now limited to a
  typed failure from reading a specific upstream artifact. Generic independent
  input failures still enter negative memory but cannot invent causal blame.
- The false quarantine was cleared through the real audited
  `cairn unquarantine` command. `cairn explain` shows both the original
  contradiction and the evidence-bearing unquarantine entry.
- The actual 20 Newsgroups snapshot was fetched in Linux and uploaded to the
  existing bucket: 3,890 documents, four categories, 3,279,707 Parquet bytes,
  SHA-256 `790c5cb1132d65ab5e47e552b356def12ae6d2dc790bda51981c1a4289780dbe`.
  S3 HEAD confirms the object at
  `datasets/20news-4cat-v1/raw.parquet`; `cairn.yaml` now names that real
  version.
- ECS task definitions also omitted `CAIRN_IMAGE_DIGEST`, so deployed
  environment manifests honestly fell back to `local-dev` and could collide
  across images. Terraform now injects the immutable container image tag into
  every task. The next worker revision applies it before the cold full run.

### CLI observations so far

- `plan` JSON: PASS; `plan --persist-graph` table: PASS after optimization.
- `unquarantine` and `explain <real env artifact>`: PASS, including real S3
  URI, inputs, decision, contradiction, and audit reversal.
- `memory why-blocked`: PASS against the live decision ledger.
- `memory search`: clean exit 1 on the host's native Bedrock/OpenSSL failure;
  no traceback, crash, hang, or guessed result. The deployed Linux path above
  separately proves the underlying account-level Titan denial.

### Current local gates before the final workload image

- Python unit/property suite: 180/180 PASS.
- Ruff: PASS. Strict mypy over 53 source files: PASS.
- Terraform CLI is not installed on this host, so `terraform fmt/validate`
  cannot be run locally; the HCL changes remain pending CI/another Terraform
  installation rather than being called green.
