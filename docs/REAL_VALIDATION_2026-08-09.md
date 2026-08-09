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

### Revision 5 cold workload and Defect 6 — stages never heartbeated

- Image `9a40594` passed an in-container live DB connection, read snapshot
  `20news-4cat-v1`, and reported `image_digest=9a40594`; it was pushed to the
  existing ECR repository at digest
  `sha256:4a7c1dd3432749fada2fda0947ec85a969d8cbff5e3d1091a45f2223b5a32b76`.
  Existing east worker, west worker, and console families were registered as
  revision 5; no infrastructure resource was created.
- Cold env task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/ef18fd96c319494690af6b54dad9ea5c`
  exited 0 and recomputed artifact
  `98235865c159a2d13559e8f5166fe940c91c284173f07ef389cd4f2be2e2a7aa`
  under the new image-dependent work key.
- Dataset task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/24da6e5b02ce41d593f2bc413839e5ad`
  exited 0, identity-reused env, read the real vendored snapshot, and
  recomputed artifact
  `e7765b30acf66f1f0e75adddb7085b9ea02946aabf9c04889fc1431c6f6bdf24`.
- Features task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/953b410cc91946229550621fe3ce54df`
  loaded all 103 real model weight tensors and performed the long stage, but
  exited 1 with `dispossessed while completing ... (fence=1)`.
- Root cause: only `claim-demo` heartbeated. The real agent loop acquired a
  45-second lease and then performed blocking compute without renewing it, so
  the reaper could fence every features run before commit.
- Fix: every claimed workload stage now owns a bounded background heartbeat
  at the protocol's real 10-second cadence. It fails closed if the fence is
  lost or renewal errors, stops before completion, and uses the same shared
  bounded pool. Unit tests prove renewal and lost-fence behavior. The failed
  revision-5 work key and its real fragments remain preserved as evidence; the
  fixed image correctly uses its own new image identity and therefore does not
  impersonate the old provenance to resume them.

### Defect 7 — identical bytes across environments collided on provenance

- Revision-6 task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/2f91250e8b4343d4885f1707013d22aa`
  recomputed byte-identical dataset output under the new image environment,
  then exited 1 because artifact
  `e7765b30acf66f1f0e75adddb7085b9ea02946aabf9c04889fc1431c6f6bdf24`
  already had the prior environment input. This happened before the new
  features stage, so it is independent of the heartbeat fix.
- PROJECT.md §4.2 explicitly defines artifact IDs as payload SHA-256 and says
  repeat completion is a no-op at the artifact primary key. The fix preserves
  that contract: direct provenance writes remain conflict-strict, while a
  fenced claim completion may converge on an already-known content address.
  The first row remains canonical provenance and both distinct work-key claims
  point to the same real artifact.
- A new live integration case completed two different work keys/environments
  against one identical content address and passed. The changed INSERT also
  passed live `EXPLAIN`; seven graph unit tests pass.

### Revision 7 real five-stage workload

- Existing worker families were advanced to revision 7 with image `0eab34d`;
  the ECR digest is
  `sha256:cbd448e1b8ece57a918ec5bfe4b8107c0217393d66be0f412045a30abac198fb`.
- The cold features task
  `arn:aws:ecs:us-east-1:357199110611:task/cairn-us-east-1/05f9aa3626ef400b83e371c84a925ad2`
  exited 0 after real transformer inference. A mid-run claim read showed its
  `updated_at` advancing and 41 seconds remaining on the 45-second lease,
  proving the new heartbeat path under actual long compute. It wrote artifact
  `f447bbf0750c6fad22b05b935962657fc4a0a4425ab9e08deaa15a601b34e033`.
- Checkpoint task `9f80af3b94c44ea5aaca1869ba11d5b7` and eval task
  `4970521d253744708151cb5e793d88cc` both exited 0, producing real artifacts
  `f26ea6a7f0a0091df2fcb95d8abf20568b335a72d5406102fbe54c2becb32f95`
  and `081d8b64f69001482c3460b2bf61a2194fb70bdfd0218ea6aa5cf90bc7fb38c5`.
- Exact `cairn run --all` task `106881c0a27c47ac809482b19c74e0d7`
  then exited 0 with identity reuse for all five stages. S3 HEAD succeeded for
  all five output objects. The console's database-derived savings endpoint
  reported 16 reused, 10 recomputed, 596.465 measured seconds saved, and its
  explicitly rate-based formula rather than a fabricated bill.

### Real crash/takeover proof and driver defects

- Two trials showed CloudWatch can buffer worker A's first line until process
  exit, so a log wait was not a valid acquisition signal. The driver now
  polls the exact `work_claims` primary-key row for `CLAIMED/RUNNING, fence=1`;
  the lookup passed live `EXPLAIN` as a one-row primary-key scan.
- A third real trial used work key `kill-worker-demo-1786229348`, task A
  `952689a51a924d59bf186c8a2cc6da83`, and task B
  `acf41a2dab4647b992999bbe189ace67`. A was stopped only after the database
  proved fence 1. B exited 0 and its exact CloudWatch stream contains
  `WON fence=2` and `COMPLETED ok=True artifact=838a3634...`.
- The verifier itself then exposed Git Bash/MSYS rewriting `/ecs/...` into a
  Windows path. `MSYS_NO_PATHCONV=1` now protects AWS resource names, and the
  script fails unless B exits 0 and both exact proof lines are present.

### TUI live/adversarial pass

- The complete non-cloud Rust workspace now passes 72 tests; the release
  binary rebuilt. Tests directly drive all advertised focus, selection,
  drill-in, zoom, shortcut, command-line, completion/history, help, Escape,
  theme, resize, and quit-confirmation paths. The multi-candidate Tab test
  confirms completion advances instead of jamming on candidate one.
- Four ignored live tests were run serially with the real CockroachDB URL and
  pinned project venv. Doctor and plan passed with real events; memory search
  surfaced the expected account/host failure without wedging.
- The live run exposed two defects. Cargo-run binaries launched from the
  crate directory could not find `cairn.yaml`; subprocesses now preserve a
  valid cwd or discover the project root from the executable (with an explicit
  override available). More importantly, a native Windows OpenSSL abort could
  bypass `stage.failed` and leave a permanent spinner. A nonzero subprocess
  exit now marks only the observed in-flight UI stage interrupted, retains the
  real Cockroach lease/countdown, and never invents a database terminal state.
  The exact abort retest rendered dataset failed and returned to a stopped
  command state.

### Browser-rendered console pass

- The deployed desktop page was rendered in Chromium and showed the real
  savings values and narrative correctly. Browser automation clicked Run the
  demo and verified the rendered replay says `writes=false` and
  `launches=false`.
- Device emulation found a real responsive bug: the graph's intentional
  `min-w-max` propagated through a Grid item's default minimum size, widening
  a 430px document to 1,065px. Shrink boundaries now keep the document at
  exactly 430px while retaining the graph's own 1,007px horizontal scroller.
  The rebuilt mobile render is unclipped.
- A real local Inspector form submission rendered a clean HTTP 503 plus the
  explanatory fallback instead of hanging or replacing it with an answer.
  The deployed Linux endpoint separately returned the real Claude Sonnet
  account denial; Titan search likewise returned the real Operation-not-
  allowed response.

### Real judge scenarios and remediation-loop defects

- A cold, real five-stage baseline under image identity `f1fca45` completed
  in 305.722 seconds and wrote genuine CockroachDB and S3 artifacts. A real
  comment-only edit in `stage_train.py` preserved all five work keys; the
  next full run identity-reused all five artifacts in 29.887 seconds.
- Changing `train.hidden_dim` from 256 to 512 produced the expected partial
  reuse: env, dataset, and features were reused; checkpoint and eval received
  new causal work keys and recomputed real artifacts in 86.355 seconds. The
  deployed graph showed the same 3/2 split as the CLI.
- The first shape-mismatch exercise found that the failure learner omitted
  the feature dimension derived by the checkpoint preflight. It also found
  that `REMEDIATE_AND_REPLAN` was not persisted and its corrected config/plan
  did not flow into eval. The learner now persists both dimensions, records a
  refusal before taking a claim, records the remediation decision, and passes
  the corrected plan to downstream stages.
- A subsequent real-cluster regression exposed a subtler over-block: the
  schema had only `embedding_dim`, so a remediation whose actual changed key
  was `train.input_dim` could make every mismatch against a 384-dimensional
  feature model look exact. Additive migration `0009_failure_input_dim.sql`
  now stores the rejected configured input separately. Blocking provenance is
  accepted only when every remediation `from` value agrees with the persisted
  failure row; the malformed historic row remains preserved but advisory.
  `EXPLAIN` passed for the migration, both changed search queries, and the
  changed signature insert. Focused live CockroachDB tests pass 8/8.
- The corrected real failure is signature
  `33495a0f-c0ed-4f63-b993-5f745d570cd0`, run
  `5eb9eef9-5b2c-419a-ae44-edf2444179ec`, with
  `embedding_dim=384`, `input_dim=768`, and the genuine `ValueError`
  traceback. Verified remediation
  `6d2fe63c-043d-4a75-bc6c-8cafbb9dd6d1` changes
  `train.input_dim: 768 -> 384`, backed by previously successful real run
  `f9e5f562-6a5c-4358-ba9a-07839f735af5`.
- Re-running the still-bad tracked config completed in 32.6 seconds without
  reacquiring the doomed key: env/dataset/features reused, checkpoint emitted
  `REFUSE_DOOMED` then `REMEDIATE_AND_REPLAN` onto corrected work key
  `51947166ca3a5b70add9f28e8ae8837d1dc39b9f892fcf98060d9325595af05f`,
  and eval reused corrected work key
  `d5c81d5f83302581816bc782b125b71d974f0d55d426a5653bb46e86841ccedd`.
  The native TUI replayed that exact NDJSON stream through its production
  reducer and renderer and showed the same five-stage outcome.

### Defect 8 — transport failures falsely accused artifacts

- A deliberately containerized scenario initially inherited a Windows-only
  AWS CA bundle path. S3 correctly failed TLS setup, but Cairn wrapped every
  upstream read exception as artifact evidence and quarantined a healthy
  dataset artifact. The artifact was explicitly unquarantined with the real
  audit reason after S3 health was reconfirmed.
- Upstream attribution now treats only `NoSuchKey`/404 (and the local
  equivalent) as evidence that an immutable artifact is unavailable. TLS,
  authentication, authorization, throttling, and transport failures propagate
  as worker-environment errors and cannot create a contradiction. Five
  focused attribution/heartbeat tests pass, including the exact TLS-shaped
  regression.

### TUI provenance correction

- Replaying the real negative-memory event stream exposed a visible false
  attribution: a rule-proposed refusal with no `authorized_by` rendered as
  `model-proposed only`. Ledger labels now distinguish authorization from
  proposal provenance. The real stream renders the refusal as
  `by rule (proposal only)` and the identity-authorized remediation as
  `by identity`.

### Defect 9 — console graph crossed scenario provenance

- The revision-9 deployed endpoint smoke returned five valid pipeline rows,
  but checkpoint paired the latest remediation decision/work key with the
  independently newest checkpoint artifact from the earlier hidden-dimension
  experiment; eval had the same cross-run mismatch. Every value was real,
  but their combination told a false causal story.
- Pipeline status now resolves `latest_artifact` through the latest decision's
  own `candidate_artifact_id`. Only a stage with no decision at all falls back
  to the newest stage artifact; a decision that cites no artifact displays no
  artifact. The integration assertion checks this invariant for every stage.

### Final image and deployed revision

- Core image `29f2986` was built from the committed source and pushed to the
  existing ECR repository at digest
  `sha256:4e76d2b29e24c07c83fe19ef83ea373ee04de621b831fe03593202f40890b1a3`.
  Its in-container doctor passed CockroachDB v26.2.5, all nine migrations,
  `fs_sem`, and AWS account `357199110611`; its real plan exited 0.
- Docker Desktop's first export failed after all build steps because C: had
  reached zero free bytes. Only regenerable Rust/npm workspace caches were
  cleaned (4.2 GB recovered); Docker/WSL was restarted and the cached build
  exported successfully. No AWS mutation happened during the failed export.
- Existing east and west worker families are revision 8 on `29f2986`. The
  console-only coherence patch was layered immutably as `bdb48fb`, ECR digest
  `sha256:c2ca39ba063a8fcf8d248f3b8bcf4c74a2a0a64d96312111fc263ba4da331b09`.
  Existing console service revision 10 reached `COMPLETED` with desired/running
  1/1, pending 0, failed tasks 0. No new service or infrastructure resource
  was created.
- The final deployed endpoint sweep passed health, all five pipeline nodes,
  paginated decisions, decision detail with 37 real artifact inputs, 50 live
  claim rows, Inspector status, savings, demo run/state/reset, and the SPA
  root. Pipeline now pairs checkpoint remediation with artifact `aa095a5...`
  and corrected eval reuse with `8115e03...`.
- Database-derived final values observed in that sweep: 53 stages reused, 17
  recomputed, 6 failures avoided, 2,553.402 measured seconds saved, formula
  `2553.4s x $0.0000274/s = $0.0700`. Judge replay exposed four recorded
  scenarios and again reported both database writes and compute launches as
  false. Titan search and Claude Inspector each returned their expected real
  HTTP 503 account-access degradation; neither guessed an answer.
