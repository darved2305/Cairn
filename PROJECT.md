# CAIRN

**Causal reuse memory for expensive compute.**

> Cairn remembers what your compute already proved, refuses work that is already running or already known to fail, and recomputes only what a change can actually affect.

---

## 0. Submission Facts

| Item | Value |
|---|---|
| Hackathon | CockroachDB × AWS Hackathon — Build with Agentic Memory |
| Submission window | 2026-06-30 10:00 ET → **2026-08-18 17:00 EDT** |
| Judging | 2026-08-19 → 2026-09-15; winners 2026-09-21 |
| Criteria (equal weight, 20% each) | Agentic Memory Design · Technological Implementation · Real-World Impact · Product Readiness · Creativity & Originality |
| License | **Apache-2.0** (`LICENSE` at repo root) |
| Repo | Public GitHub, created inside the submission window, no pre-existing code |
| Demo | Public URL (CloudFront → ALB → ECS Fargate), read-only judge mode, no login |
| Video | ≤ 2:55, YouTube, unlisted-but-public |
| CockroachDB tools used | **4 of 4** — Distributed Vector Indexing (C-SPANN), Cloud Managed MCP Server, `ccloud` CLI, Agent Skills Repo |
| AWS services used | **6** — Bedrock, ECS Fargate, S3, Lambda, EventBridge, CloudWatch |

Stage-one judging is pass/fail on theme + required APIs. Every item above is a hard gate; §9 is the checklist that proves each one.

---

## 1. The Problem, Stated Precisely

Teams that run ML and data pipelines waste compute in three distinct ways. They are not the same problem and they do not have the same fix.

### 1.1 Unnecessary recomputation

Existing caches — Make, Bazel, DVC, Metaflow, W&B artifacts, Airflow/Dagster asset caches, `functools.lru_cache`, Docker layer cache — are **declared-input hashers**. They compute a key over the declared inputs and invalidate when the key changes. This is correct and conservative, and it is the right default. It is also lossy in a specific, measurable way:

> A change to a declared input is treated as proof of invalidation. It is not. It is only *evidence of possible* invalidation.

Concrete cases that these systems invalidate and that a careful engineer would not:

| Change | Declared-input cache | Reality |
|---|---|---|
| Docstring added to `train.py` | Invalidates checkpoint | Cannot affect the checkpoint |
| `logger.debug(...)` added inside the training loop | Invalidates checkpoint | Cannot affect the checkpoint (logging has no return-value effect on the computation) |
| `eval.py` rewritten | Invalidates the **whole pipeline** if the cache key is "repo tree hash" | Cannot affect the feature table or the checkpoint — they are upstream |
| Private helper `_fmt_row` renamed in `dataset.py`, all 3 call sites updated | Invalidates the feature table | Cannot affect the feature table if the symbol is not reachable from the feature entrypoint and behaviour is unchanged |
| `eval.batch_size` changed in `config.yaml` | Invalidates everything keyed on `config.yaml` | Affects evaluation throughput only |

The dominant real-world failure mode is **key granularity**: teams key on a directory hash, a Git SHA, or a whole config file because per-symbol keying is tedious to maintain. The cost is that a one-line comment change re-runs an eight-hour job.

### 1.2 Concurrent duplicate execution

The same expensive stage is launched twice, concurrently, from two places: a developer's laptop and a CI runner; two CI runners on two pushes 40 seconds apart; a retried job whose predecessor is still alive. Local caches cannot see each other. Object-store caches ("does `s3://.../<key>` exist?") have a race window equal to the entire job duration: both workers check, both miss, both run, both write.

This is a **distributed mutual-exclusion problem with fault tolerance**, and object stores do not solve it. Neither does an in-memory lock, a file lock, or a "check S3 then write S3" pattern. It requires a transactional store with strong isolation and a lease that survives worker death.

### 1.3 Repeated known failures

A configuration is launched, fails after burning real compute, is fixed locally, and the fix is never written down anywhere a machine can read. Three weeks later a teammate — or the same engineer on a different branch — launches a configuration that differs cosmetically and fails identically. The failure knowledge exists (a Slack thread, a scrollback buffer, a closed PR comment) but it is not queryable, so the compute is spent again.

### 1.4 Why this is worth building now

The three wastes share one missing substrate: **a durable, transactional, queryable memory of computational work that is shared across machines**. Once that substrate exists, all three become tractable, and they compose — the same causal graph that enables partial reuse is what identifies which worker owns which node, and the same run records that support crash resume are what supply structured failure features.

---

## 2. Primary User

**Priya Raghunathan**, ML platform engineer at a 14-person company. Four ML engineers, no dedicated infra hire; Priya *is* the infra function, at roughly 50% of her time.

Her environment:

- One monorepo. Feature generation, training, evaluation as Python entrypoints, orchestrated by a `Makefile` and a GitHub Actions workflow.
- Compute is a mixture: laptops (M-series, CPU), GitHub Actions runners, and AWS — ECS Fargate for CPU stages, a small GPU fleet for training.
- Compute is a line item she is asked about monthly. Nobody can currently answer "what did we spend re-running work we already had?"
- Correctness is non-negotiable. Her team ships model artifacts into a product. A silently stale checkpoint served as fresh is a worse outcome than any amount of wasted compute, and she knows it.

Her actual week, as observed behaviour rather than narrative:

- She has a `SKIP_FEATURES=1` environment variable in the Makefile. It exists because the feature stage takes 22 minutes and is usually not affected by what she is changing. It is a manual, undocumented, unaudited reuse decision made by a human under time pressure. It has been wrong twice.
- Two of her engineers pushed to the same branch 40 seconds apart last month. Two CI runs each launched the same training job. Both completed. One artifact overwrote the other. Nobody noticed for two days.
- She keeps a file called `notes/gotchas.md`. It has 31 lines. Line 12 says `mpnet needs input_dim=768 in classifier cfg — spent 2h on this`. A new engineer hit the identical error six weeks later, because nobody greps a file they don't know exists.

**Cairn's job is to make `SKIP_FEATURES=1` unnecessary, safe, and auditable — and to make `notes/gotchas.md` a database that the agent reads before it launches anything.**

What Cairn must therefore be:

- Adoptable inside an existing repo, without rewriting the pipeline into a new DSL.
- Never silently wrong. It must be able to say *"I do not know, so I am recomputing."*
- Able to show its work, because Priya has to defend the decision to a skeptical staff engineer.

---

## 3. The Central Technical Insight

> Deciding whether a specific change invalidates a specific artifact is a **causal-impact** question, not only a content-hashing question.

Content hashing answers *"did any declared input change?"* Causal impact answers *"can this change reach this artifact, and if it can, does it alter it?"* The second question has more available evidence than the first: the structure of the code, the recorded dependency graph of the previous run, and — decisively — the ability to *recompute a bounded sample and compare*.

Cairn combines five sources of evidence, in a strict authority order:

1. **Deterministic structural analysis** — module import graph and symbol reference graph, extracted from Python ASTs. Answers reachability.
2. **Recorded causal dependency edges** — what the previous successful run actually read, at file/symbol/partition granularity, written transactionally at the time it ran.
3. **Model-assisted reasoning (Bedrock Claude)** — proposes a hypothesis for ambiguous changes and selects which probe would discriminate.
4. **A cheap deterministic validation probe** — recompute a bounded, deterministically-selected sample; compare canonical bytes.
5. **Transactional recording** of the decision, its evidence, and its authority — plus immediate quarantine when contradictory evidence arrives.

### 3.1 The safety rule

> **The model may propose reuse. Deterministic evidence must authorize it.**

This is enforced structurally, not by convention. The reuse path is:

```
propose(LLM | rule) → select_probe → run_probe → verdict(deterministic) → commit_decision
```

`commit_decision` writes `authorized_by ∈ {'probe', 'structural', 'identity'}`. There is no enum value for `'model'`. A proposal that reaches `commit_decision` without a passing probe or a passing structural proof is written as `verdict='refused'` and the stage recomputes. The database `CHECK` constraint makes an LLM-authorized reuse unrepresentable:

```sql
CHECK (verdict <> 'reuse' OR authorized_by IN ('probe','structural','identity'))
```

### 3.2 Conservative mode is the default

`CAIRN_MODE=conservative` is the default and is what the demo runs.

In conservative mode:

- If no probe is defined for an artifact class → **recompute**.
- If the environment fingerprint differs at all → **recompute** (no probe is trusted across environments).
- If a change touches a symbol that is reachable from the stage entrypoint → **recompute**, regardless of what the model proposes.
- If the probe is not bit-exact → **recompute**. Conservative mode uses `atol = 0`.
- If any input is unclassifiable (a binary blob, a generated file with no recorded provenance) → **recompute**.
- Upstream invalidation is transitive and unconditional: if a node recomputes, every downstream node is invalidated unless its own probe passes against the *new* upstream artifact.

`CAIRN_MODE=balanced` exists in the codebase and is documented, but is off by default, requires an explicit flag, and is not used in the demo or video. It permits a documented tolerance on the checkpoint logit probe. **The judged artifact is conservative mode.**

### 3.3 Change classes and their required verification

No class is asserted safe. Each has an explicit, mechanical proof obligation. If the obligation cannot be discharged, the class does not apply and the stage recomputes.

| Class | Proof obligation | Guarantee | Explicit non-guarantee |
|---|---|---|---|
| `comment_only` | Normalize both file versions by parsing to AST and re-emitting with `ast.unparse` on a canonicalized tree; the two normalized forms must be byte-identical. | The compiled semantics of the module are unchanged (docstrings excluded — see below). | Does **not** hold if a docstring is read at runtime (`__doc__`, `inspect.getdoc`, doctest). Cairn detects `__doc__`/`inspect` usage anywhere in the reachable set and, if present, refuses the class. |
| `formatting_only` | Same AST-normalization equality as above. | Same as `comment_only`. | Does not cover files parsed as text by the program itself (e.g. a `.py` read via `open()`). Detected by scanning reachable code for reads of `.py` paths; refuses if found. |
| `logging_only` | Diff is confined to statements that are calls whose receiver resolves to a module-level `logging.Logger` binding, and whose value is discarded (expression statement). No f-string in the call may contain a walrus or a call with side effects — enforced by AST inspection of the argument expressions. | The change adds no data flow into any live variable. | Does not cover `logging` handlers that mutate state, `print` redirected into a captured stream that is later consumed, or logging that changes timing in a race-sensitive program. Cairn refuses this class if the reachable set spawns threads or processes. |
| `private_symbol_rename` | Old name matches `_[a-z]`; every reference to the old name in the reachable set is rewritten; after α-renaming the new name back to the old across the changed files, the normalized ASTs are byte-identical. No `getattr`/`setattr`/`globals()`/`vars()`/`eval`/`exec`/`importlib` call appears anywhere in the reachable set. | Reference-for-reference equivalence under renaming. | Dynamic attribute access defeats it entirely; hence the hard refusal on those builtins. |
| `unreachable_change` | The changed symbol's node is not reachable in the call/import graph from the stage entrypoint, computed over the *union* of the old and new graphs. | The changed code cannot execute during this stage. | Python's dynamic import and dispatch make static reachability an *approximation*. Cairn therefore refuses whenever `importlib`, `__import__`, plugin-registry decorators, or entry-point loading appear in the reachable set, and treats a reachability proof as valid only under that restriction. |
| `downstream_only_config` | The changed config keys are not present in the recorded `artifact_inputs` for this artifact, and the artifact's recorded config-read set was captured by an instrumented config accessor (not by hashing the whole file). | The artifact never read those keys. | Requires that the previous run used the instrumented accessor; if provenance is missing, the class does not apply. |

**A probe is still run on top of the structural proof for `features` and `checkpoint`.** Structural proof alone authorizes reuse only for `eval`-stage config changes and for the `unreachable_change` class where a probe is impossible. This is a deliberate belt-and-braces choice: structural analysis of Python is an approximation, and the probe is cheap.

---

## 4. The Five Headline Behaviours

### 4.1 Negative computational memory

Every failed run writes a **failure signature**: a structured feature vector plus a natural-language summary embedded as a 1024-dimensional vector (Amazon Titan Text Embeddings V2, `normalize=true`).

Structured features (columns, indexed, exact-matchable):

`workload_kind` · `stage` · `model_family` · `model_id` · `embedding_dim` · `num_labels` · `dataset_rows` · `max_seq_len` · `batch_size` · `grad_accum` · `precision` · `optimizer` · `lr` · `instance_kind` · `vcpu` · `mem_mib` · `accelerator` · `framework` · `framework_version` · `error_class` · `error_module` · `exit_code` · `oom_killed`

Semantic features: `summary_text` (a normalized one-paragraph description of the failure, generated deterministically from the traceback by a template, then refined by Claude) → `embedding VECTOR(1024)`.

**Match tiers**, and what each is allowed to do:

| Tier | Condition | Agent action |
|---|---|---|
| `exact` | All *causal* structured features equal (`error_class` + the config keys that appear in the recorded remediation's `changed_keys`), same `framework_version`, same `instance_kind`. | **Refuse the plan.** Propose the recorded remediation. Do not launch. |
| `strong_semantic` | Cosine distance ≤ 0.15 **and** `stage` + `error_class` equal **and** at least one causal structured feature matches exactly. | **Refuse the plan**, present evidence and the prior remediation, require the plan be modified or an explicit `--override` given. |
| `weak` | Cosine distance ≤ 0.35, no structured agreement beyond `stage`. | **Advisory only.** Surface as a warning in the plan output. **Never blocks.** |

The tiering is the point. Vector similarity alone must not gate execution — a `weak` match is a hint to a human, not a decision. This is stated in the UI next to every weak match.

**The full refuse → remediate → re-evaluate → claim → launch loop:**

1. Agent builds the plan and computes the structured feature vector for the *proposed* run.
2. Structured pre-filter query on the exact-match columns (`WHERE stage=$1 AND error_class IS NOT NULL AND ...`).
3. Vector search restricted by the same prefix columns, `ORDER BY embedding <=> $q LIMIT 8`.
4. Tier the results. On `exact` or `strong_semantic`: **halt before any claim is taken.**
5. Present the remembered failure: original traceback head, when, on what config, and the remediation that succeeded (`remediations` row, joined).
6. Emit a concrete remediation as a config diff (e.g. `train.classifier.input_dim: 384 → 768`).
7. Apply the diff to the plan, **recompute the work key and the feature vector**, and re-run steps 2–4 against the *modified* plan.
8. Only when the modified plan produces no `exact`/`strong_semantic` match does Cairn acquire the work claim and launch.

Step 7 is the part that makes this agentic rather than a lookup table: the remediated plan is re-evaluated against memory, including against failures caused by the remediation itself.

### 4.2 Transactional duplicate-work prevention

Every expensive stage has a deterministic `work_key`:

```
work_key = sha256(canonical_json({
  "v": 1,
  "stage": stage,
  "code_fingerprint":   <merkle root over the reachable symbol set>,
  "data_fingerprint":   <content hash of the input dataset partition set>,
  "config_fingerprint": <hash over the config keys this stage actually reads>,
  "env_fingerprint":    <hash of {base image digest, python version, sorted pip freeze, torch threads}>,
  "upstream":           [sorted artifact_ids of upstream inputs]
}))
```

Identical semantic work on any machine in any region produces the identical key. That is what makes cross-machine deduplication possible at all.

The claim protocol, in full:

- **Acquire.** A single `SERIALIZABLE` transaction: `INSERT INTO work_claims (...) VALUES (...) ON CONFLICT (work_key) DO NOTHING RETURNING fence`. Empty result ⇒ a row exists ⇒ re-read it `FOR UPDATE` and branch on its state.
- **States.** `CLAIMED → RUNNING → SUCCEEDED | FAILED | ABANDONED`. `SUCCEEDED` rows are retained as the reuse index. `ABANDONED` is only ever written by the reaper.
- **Fencing.** `fence BIGINT NOT NULL DEFAULT 1`. It increases by exactly 1 on every ownership transfer. Every subsequent write by a worker carries `WHERE work_key=$1 AND owner_id=$2 AND fence=$3`. A resurrected worker with a stale fence updates zero rows, detects the zero row count, and terminates without writing.
- **Heartbeat.** `lease_expires_at = now() + interval '45 seconds'`, refreshed every 10s by a background thread. The heartbeat `UPDATE` is itself fenced; if it affects zero rows the worker knows it has been dispossessed and self-terminates immediately.
- **Expiry and safe takeover.** A takeover is permitted only when `lease_expires_at < now()` **and** `state IN ('CLAIMED','RUNNING')`. The takeover transaction sets `owner_id`, bumps `fence`, and writes an `ownership_transfers` audit row — atomically, serializably. Exactly one contender wins by construction.
- **Loser behaviour.** The loser does **not** error. It reads owner identity, host, region, start time, and current stage progress; renders a live "waiting on `worker-b` (ECS task `…/3f2a`, us-east-1, running 41s, fragment 2/3)" panel; polls `work_claims` + `run_fragments` at 1s; and on `SUCCEEDED` reads `artifact_id` and returns the artifact to its caller as its own result. From the caller's point of view it computed the value.
- **Idempotency.** Artifact writes go to a content-addressed S3 key. Completion is a single serializable transaction that inserts the `artifacts` row and flips the claim to `SUCCEEDED` together. Re-running completion is a no-op because the `artifacts` primary key is the content address.
- **Retries.** Every transaction runs inside a retry wrapper that catches SQLSTATE `40001`, backs off exponentially with jitter (50ms base, ×2, ±25%, cap 2s, 8 attempts), and re-executes the *entire* transaction closure. Transaction bodies are pure functions of their inputs so replay is safe.
- **Cancellation.** `UPDATE work_claims SET state='ABANDONED', cancel_requested=true` — the worker observes `cancel_requested` on its next heartbeat and exits at the next fragment boundary.
- **Split-brain.** Prevented by the conjunction of serializable isolation on the claim row and the fence on every subsequent write. There is no code path in which two workers hold the same `(work_key, fence)`.

There is no in-memory lock anywhere in Cairn. The demo proves this by racing two workers in two AWS regions against one cluster.

### 4.3 Causal partial reuse

The pipeline DAG:

```
env → dataset → features → checkpoint → eval
```

Each node is an artifact with recorded typed input edges. On a change, Cairn walks the DAG in topological order and classifies each node independently. Worked examples the demo actually executes:

| Change | env | dataset | features | checkpoint | eval |
|---|---|---|---|---|---|
| Comment + `logger.debug` in `train.py` | reuse | reuse | reuse | **reuse** (structural + logit probe) | reuse |
| `eval.metrics += ["macro_f1"]` | reuse | reuse | reuse | reuse | **recompute** |
| `train.hidden_dim: 256 → 512` | reuse | reuse | **reuse** | **recompute** | recompute |
| Embedding model → `all-mpnet-base-v2` | reuse | reuse | **recompute** | recompute | recompute |
| `pip install` bumps `torch` | **recompute** | reuse | recompute | recompute | recompute |

The third row is the money shot: the feature stage — the expensive one — is reused across an architecture change, because the architecture is not in the feature stage's recorded read set.

Every node in the UI shows: verdict, the class that applied, the probe that ran, the probe's runtime, and the exact `artifact_inputs` edges that were consulted. A judge can click any green node and see why it was green.

### 4.4 Evidence-backed reuse

Six probe types. For each: what it does, what it proves, and — stated with equal prominence — what it does not.

**P1 `env_identity`** — compare `env_fingerprint`.
*Proves:* base image digest, Python version, and the full resolved dependency set are identical.
*Does not prove:* identical CPU microarchitecture, identical BLAS kernel selection, or absence of nondeterminism from thread scheduling. Cairn pins `OMP_NUM_THREADS=1`, `torch.use_deterministic_algorithms(True)`, and `PYTHONHASHSEED=0` in the container so that this gap is closed *for the workload Cairn runs*. It is **not** closed in general, and the docs say so.

**P2 `structural_unreachable`** — reachability over the union of old and new graphs.
*Proves:* under the stated restriction (no `importlib`/`__import__`/`getattr`-dispatch/entry-points in the reachable set), the changed code cannot be executed by this stage.
*Does not prove:* anything if that restriction is violated — which is why violation is a hard refusal, not a warning.

**P3 `feature_resample`** — recompute `k = 64` rows chosen by `sha256(artifact_id ‖ row_id) mod N`, compare `sha256` of the canonical little-endian float32 byte encoding.
*Proves:* those 64 rows are bit-identical.
*Does not prove:* the other 2,336 rows are. This is a **sampling** argument. Cairn states the sample size and the population in the UI, next to the word "sample". It does not present it as a proof of full-table equality. Combined with a passing P1 and a passing structural class, the residual risk is that a change altered the computation for some rows but not for a hash-selected 64 — which for the classes Cairn admits (comment/format/logging/unreachable) requires the structural proof to have been wrong, i.e. requires a dynamic-dispatch escape that P2 already refuses on.

**P4 `checkpoint_logit`** — load the checkpoint, run a fixed 128-example batch (deterministic selection, `model.eval()`, `torch.inference_mode()`), compare logits.
*Conservative mode:* `atol = 0`, bitwise. Justified only because P1 passed and determinism is pinned.
*Proves:* the checkpoint produces identical outputs on those 128 inputs.
*Does not prove:* identical outputs on all inputs, and does not detect a change that alters behaviour only outside the probe batch.

**P5 `schema_stats`** — schema (names, dtypes, order), row count, and a per-column checksum over a deterministic 10% partition slice.
*Proves:* structural conformance and slice-level content equality.
*Does not prove:* content equality outside the slice.

**P6 `eval_slice_replay`** — recompute the metric on a fixed 200-example slice, compare bitwise.
*Proves:* the evaluation code produces the same number on the same slice.
*Does not prove:* the full-set metric is unchanged.

The `probe_runs` table records `probe_type`, `sample_spec` (the exact selection rule), `population_size`, `sample_size`, `tolerance`, `runtime_ms`, `passed`, and `evidence_digest`. The UI renders sample/population as a fraction, always. **Cairn never claims a probe proves full equivalence.**

### 4.5 Crash recovery

Work is decomposed into **fragments** — checkpointable boundaries within a stage. `features` fragments by shard (3 shards × 800 documents); `checkpoint` fragments by epoch.

On completion of a fragment the worker writes, in one serializable transaction: the fragment's S3 URI, its content hash, its index, and a heartbeat — all fenced.

On worker death:
1. Heartbeats stop; `lease_expires_at` passes.
2. The reaper (EventBridge → Lambda, every 30s) finds expired leases and marks them takeover-eligible. It does **not** delete anything.
3. The next contender takes over in a serializable transaction, bumping `fence`.
4. The new owner reads `run_fragments` for the work key, validates each fragment's content hash against S3, and resumes from `max(index) + 1`.
5. The dead worker, if it revives, attempts a fenced write, updates zero rows, logs `dispossessed`, and exits. It cannot overwrite the new owner's result.

The demo kills a worker mid-`features` (after shard 1 of 3) with `docker kill` / ECS `StopTask` and shows recovery resuming at shard 2, with the wall-clock saving displayed.

---

## 5. The Workload — Real, Small, Reproducible

### 5.1 Selection

Six candidates were compared against: reliability, setup friction, runtime, cost, determinism, demo clarity, controllable failure modes, AWS fit, licensing, download size, cold start.

| Candidate | Verdict |
|---|---|
| HF Transformers fine-tune (BERT-base) on GPU | Rejected — GPU quota risk, 10+ min per run, cold start dominates a 3-minute video, cost unpredictable |
| Image feature extraction + classifier (ResNet/CIFAR) | Rejected — 170MB download, image determinism across BLAS versions is fiddly |
| XGBoost with an expensive feature stage | Rejected — feature stage would have to be synthetic to be expensive; synthetic compute is banned by our own rules |
| LLM-as-judge eval pipeline | Rejected — nondeterministic by construction, so reuse cannot be verified |
| SageMaker training job | Rejected — cold start 3–5 min, kills the demo |
| **Sentence-transformer embeddings + PyTorch classifier + eval, on ECS Fargate CPU** | **Selected** |

### 5.2 The selected pipeline

| Stage | What it really does | Real runtime (2 vCPU / 4 GiB Fargate) | Output |
|---|---|---|---|
| `env` | Resolve lockfile, capture image digest, `pip freeze`, torch config | ~2 s | env fingerprint row |
| `dataset` | Load 20 Newsgroups (4 categories: `sci.space`, `rec.autos`, `comp.graphics`, `talk.politics.mideast`), strip headers/footers/quotes, normalize whitespace, stable sort, split | ~9 s | Parquet → S3 |
| `features` | `sentence-transformers/all-MiniLM-L6-v2`, 384-dim, fp32, batch 32, `max_seq_length=256`, 3 shards × ~800 docs | **~95 s** | float32 array → S3 |
| `checkpoint` | 2-layer MLP (384→256→4), AdamW, 12 epochs, seed 1337, deterministic algorithms on | **~28 s** | `state_dict` → S3 |
| `eval` | Accuracy + macro-F1 on held-out split | ~4 s | JSON → S3 |

Total cold pipeline: **~2 min 18 s**. Fully warm: **~4 s** (probes only). Partial (architecture change): **~34 s**.

That spread — 138s / 34s / 4s — is what makes the demo legible in under three minutes without a single `sleep()`.

- **Dataset licensing.** 20 Newsgroups is a long-standing public research corpus, redistributed by scikit-learn. Cairn vendors a fixed snapshot to `s3://<bucket>/datasets/20news-4cat-v1/` at setup time so the demo never depends on an external fetch. Provenance and license notes are in `data/DATASET.md`.
- **Model licensing.** `all-MiniLM-L6-v2` is Apache-2.0. Vendored into the image at build time — no runtime model download.
- **Determinism.** `PYTHONHASHSEED=0`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `torch.manual_seed(1337)`, `torch.use_deterministic_algorithms(True)`, sorted input order, fixed batch composition. Verified by CI: same image + same inputs ⇒ bit-identical outputs, asserted over 3 runs.

### 5.3 The real failures Cairn remembers

Three genuine failures. None is scripted, none is a hardcoded string; all are produced by actually running an invalid configuration and catching the real exception.

**F1 — label-space mismatch.** `train.num_labels: 4 → 6` while the feature table has 4 classes.
Real result: `IndexError: Target 5 is out of bounds.` from `nn.CrossEntropyLoss`. Cost: ~3 s. Remediation: `num_labels → 4`.

**F2 — embedding dimension mismatch.** Embedding model → `all-mpnet-base-v2` (768-d) without updating `train.classifier.input_dim` (384).
Real result: `RuntimeError: mat1 and mat2 shapes cannot be multiplied (32x768 and 384x256)`. Cost: ~2 s after a ~60 s feature stage. Remediation: `input_dim → 768`. **This is Priya's `notes/gotchas.md` line 12, made machine-readable.**

**F3 — bounded memory exhaustion.** `features.batch_size: 4096` with `max_seq_length: 512`.
Real result: a genuine allocation failure. To keep it *bounded and catchable*, the container sets `RLIMIT_AS` to 3 GiB, so the allocator raises a catchable Python-level error instead of the task being SIGKILLed by the kernel OOM killer. **This is disclosed in the README and in the UI**: the memory ceiling is enforced by `setrlimit`, and the failure is a real refused allocation under that ceiling, not a simulated one. Remediation: `batch_size → 32`.

F1 and F2 are the demo path (fast, unambiguous, real traceback). F3 is seeded into memory during setup and used to demonstrate `strong_semantic` matching on a near-miss config (`batch_size: 3072`).

### 5.4 Measurement honesty

The UI shows only measured values, plus clearly-labelled arithmetic on them:

**Measured:** stage wall-clock (ms), probe wall-clock (ms), artifact bytes, S3 key, vCPU/memory allocation, region, ECS task ARN, stages reused, stages recomputed, duplicate launches prevented (count of losers that subscribed), failures avoided (count of `exact`/`strong_semantic` refusals), fragments resumed.

**Derived, and labelled `rate-based`:** cost = `duration_s × rate_usd_per_second`, where rates live in a `cost_rates` table seeded with published AWS Fargate on-demand us-east-1 pricing and editable by the user. The UI renders the formula inline: `95.2 s × $0.0000274/s = $0.0026`.

**Extrapolation, and labelled `EXTRAPOLATION — not observed`:** a separate, visually distinct panel, off by default, that multiplies observed reuse ratios by a user-supplied fleet size and GPU rate. It carries the sentence *"This is arithmetic on your inputs, not a measurement of this system."*

**Never shown:** an invented dollar figure presented as an observation. There is no code path that produces one.

---

## 6. Architecture

### 6.1 Components

```
┌─────────────────────────────────────────────────────────────────────┐
│  cairn CLI (local, laptop)   ·   GitHub Actions (CI)                │
│  `cairn run features`  ·  `cairn plan`  ·  `cairn explain`          │
└───────────────┬─────────────────────────────────┬───────────────────┘
                │ pgwire (TLS)                    │ pgwire (TLS)
                ▼                                 ▼
        ╔═══════════════════════════════════════════════════╗
        ║   CockroachDB Cloud  (SERIALIZABLE, multi-region)  ║
        ║   artifacts · artifact_inputs · code_units         ║
        ║   work_claims · runs · run_fragments               ║
        ║   failure_signatures (VECTOR(1024) + C-SPANN idx)  ║
        ║   remediations · reuse_decisions · probe_runs      ║
        ║   contradictions · ownership_transfers · cost_rates║
        ╚═══════╦═══════════════╦═══════════════════╦════════╝
                │               │                   │
   ┌────────────▼───┐  ┌────────▼─────────┐  ┌──────▼─────────────────┐
   │ ECS Fargate    │  │ Lambda (reaper)  │  │ Cairn Console (ECS)    │
   │ worker tasks   │  │ EventBridge 30s  │  │ FastAPI + React        │
   │ us-east-1      │  │ lease expiry     │  │ ALB → CloudFront       │
   │ us-west-2      │  └──────────────────┘  │ = public demo URL      │
   └────┬───────┬───┘                        └──────┬─────────────────┘
        │       │                                   │
        │       └────► Amazon Bedrock ◄─────────────┘
        │              · anthropic.claude-sonnet-5  (agent reasoning)
        │              · amazon.titan-embed-text-v2:0 (1024-d, normalized)
        ▼
   ┌────────────────────────┐        ┌──────────────────────────────────┐
   │ Amazon S3              │        │ CockroachDB Cloud MCP Server     │
   │ content-addressed      │        │ https://cockroachlabs.cloud/mcp  │
   │ artifacts + fragments  │        │ ← Memory Inspector (NL → SQL)    │
   └────────────────────────┘        └──────────────────────────────────┘
```

### 6.2 CockroachDB tools — what each one does, and why it is load-bearing

**1. Distributed Vector Indexing (C-SPANN).** `failure_signatures.embedding VECTOR(1024)` with

```sql
CREATE VECTOR INDEX fs_sem ON failure_signatures (stage, error_class, embedding vector_cosine_ops);
```

Prefix columns `(stage, error_class)` are used because CockroachDB only accelerates filtered vector search when the filters match prefix columns. This powers the `strong_semantic` tier: a *new* failure phrased differently from a remembered one still matches. Visible behaviour: Cairn blocks a config it has never seen, because it is semantically the same mistake as one it has.
**Why in CockroachDB rather than a vector DB:** the vector search result is joined, in the *same transaction*, against `remediations`, `runs`, and `work_claims` to produce an actionable decision. Vectors, relational rows, and the claim state must be transactionally consistent — a remediation that is committed but whose embedding is not yet visible would produce a wrong decision. One store, one transaction, no dual-write.

**2. Cloud Managed MCP Server** (`https://cockroachlabs.cloud/mcp`). The **Memory Inspector** panel in the console is an agent (Bedrock Claude) whose tools are the MCP server's `list_tables`, `get_table_schema`, `select_query`, and `explain_query`. A judge types *"which failures did Cairn refuse in the last hour, and what remediation did it propose?"* and gets a real answer from a real query against the live cluster, with the executed SQL shown. Read-only: the console binds a service account restricted to `SELECT`. Constraints respected in the UI (20 s query timeout, 10 KiB response cap, 25-row default limit, no `crdb_internal`).
**Why load-bearing:** the memory is only useful if it is interrogable. This turns "we have a database" into "you can ask the memory questions", which is the demonstrable difference between storage and memory.

**3. `ccloud` CLI.** Used in three places, all real:
- `make cluster` — `ccloud cluster create`, `ccloud cluster sql --url` for the connection string, `ccloud cluster user create` for the app role; teardown via `ccloud cluster delete`. Provisioning is scripted, reproducible, and disposable.
- CI — a service-account API key drives `ccloud` non-interactively to create an **ephemeral verification cluster** per pull request, apply migrations, run the full integration suite (including the race test) against real CockroachDB, and delete the cluster. No mocks in the concurrency tests.
- Runtime — `cairn doctor` shells `ccloud cluster list --json` to report cluster identity, regions, and plan alongside Cairn's own health checks, so an operator sees the memory layer's topology from inside the product.

**4. Agent Skills Repo** (`cockroachlabs/cockroachdb-skills`, Apache-2.0). Installed at `.agents/skills/` via `npx skills add cockroachlabs/cockroachdb-skills` and committed. Cairn's own contribution guide routes work through them: the retry/serializable-transaction and observability skills are the reference for `cairn/db/txn.py`, and `CONTRIBUTING.md` instructs agents to load the performance and resilience skills before touching schema or transaction code. `docs/SKILLS_USAGE.md` records exactly which skills informed which files — including the two schema changes they caused (see §10).

### 6.3 AWS services — what each one does

| Service | Role | Load-bearing because |
|---|---|---|
| **Amazon Bedrock** | `anthropic.claude-sonnet-5` for change classification, probe selection, remediation authoring, and the Memory Inspector agent. `amazon.titan-embed-text-v2:0` (1024-d, `normalize=true`) for failure embeddings. | The classification of an ambiguous diff and the authoring of a remediation are the two genuinely non-deterministic judgments in the system. Embeddings are what make `strong_semantic` matching exist at all. |
| **ECS Fargate** | Runs the real workload as tasks in **two regions** (us-east-1, us-west-2) and hosts the console. | Two regions against one cluster is what makes the claim race a real distributed race rather than two processes on one box. |
| **Amazon S3** | Content-addressed artifact and fragment storage; vendored dataset and model snapshots. | Reuse is only meaningful if the artifact is actually fetchable by a different machine. Fragment durability is what makes crash resume real. |
| **AWS Lambda** | Lease reaper. Marks expired leases takeover-eligible. Also the `cairn-webhook` endpoint for CI plan comments. | The reaper must run when *no worker is alive*. It cannot live in the worker. |
| **Amazon EventBridge** | 30-second schedule for the reaper. | Bounded takeover latency. |
| **Amazon CloudWatch** | Structured JSON logs, custom metrics (`ReuseRate`, `DuplicatesPrevented`, `FailuresAvoided`, `ProbeLatencyP50`, `TxnRetries40001`), 4 alarms. | Product readiness is a judged criterion; observability is not optional. |

CloudFront fronts the ALB for the public demo URL and TLS.

### 6.4 The agent loop

Cairn is agentic in the operational sense: on every invocation it perceives state, consults memory, decides among genuinely different actions, acts, and writes back what it learned.

```
perceive   → git diff, config diff, env fingerprint, dataset fingerprint
recall     → causal graph (SQL) + negative memory (structured + vector)
decide     → one of nine actions
act        → claim / subscribe / probe / launch / resume / refuse
learn      → write decision, probe, fragments, artifact, failure signature, remediation
```

The nine actions, each of which is a distinct code path with a distinct database effect:

1. `REUSE` — artifact returned; `reuse_decisions` written with `authorized_by`.
2. `PARTIAL_REUSE` — subset of DAG reused, remainder recomputed.
3. `RECOMPUTE` — causal dependency changed.
4. `REFUSE_DUPLICATE` — another worker owns this key.
5. `SUBSCRIBE` — wait on the owner and adopt its artifact.
6. `REFUSE_DOOMED` — negative memory match at `exact`/`strong_semantic`.
7. `REMEDIATE_AND_REPLAN` — apply remediation, recompute key, re-evaluate.
8. `RESUME` — take over expired lease, resume from fragments.
9. `ESCALATE` — ask the human. Triggered by: projected cost > `CAIRN_APPROVAL_USD` (default $0.50 in demo config), a destructive `--override` of a `strong_semantic` refusal, or a quarantine event.

Approval is required **only** for (9). Everything else is autonomous — which is the point: an agent that asks permission for every decision is a wizard, not an agent.

### 6.5 Contradiction handling

If a later run produces evidence that contradicts an earlier reuse — the classic case being that a downstream stage fails in a way whose signature implicates a reused upstream artifact — Cairn:

1. Writes a `contradictions` row (reused artifact id, contradicting run id, evidence).
2. Sets `artifacts.quarantined_at = now()`. Quarantined artifacts are never reused again.
3. Invalidates every `reuse_decision` that cited that artifact, transitively marking downstream artifacts `suspect`.
4. Raises a CloudWatch alarm and surfaces a red banner in the console.

Quarantine is one-way and requires an explicit human `cairn unquarantine <artifact_id> --reason` to reverse, which is itself an audited row. This is the mechanism that makes "the model may propose reuse" survivable: if the deterministic authority was ever wrong, the system finds out and stops trusting the artifact.

---

## 7. Product Surface

### 7.1 CLI

```
cairn init                     # scaffold cairn.yaml, register stages
cairn plan [stage]             # decisions + evidence, no execution, exit 0/1
cairn run <stage> [--all]      # the agent loop
cairn explain <artifact_id>    # full provenance + decision chain
cairn memory search "<text>"   # query negative memory directly
cairn memory why-blocked       # explain the last refusal
cairn doctor                   # cluster (via ccloud), AWS, schema, index health
cairn unquarantine <id> --reason "<text>"
```

`cairn plan` exits non-zero on a `REFUSE_DOOMED`, which is what makes it usable as a CI gate: the pipeline stops before spending money on a run that memory says will fail.

### 7.2 Console (the demo URL)

Five panels, all reading live from CockroachDB:

1. **Causal Graph** — the five-node DAG, colour-coded by verdict, click-through to per-node evidence: class applied, probe type, sample/population, runtime, and the `artifact_inputs` edges consulted.
2. **Decision Ledger** — append-only stream of every decision with actor (`agent` / `probe` / `human`), authority, and latency.
3. **Claim Theatre** — live `work_claims` view during the race: both workers, their regions, ECS task ARNs, who won, the fence value, and the loser's subscription progress.
4. **Negative Memory** — searchable. Shows tier, cosine distance, the structured features that matched, the original traceback head, and the remediation. Weak matches are visually distinct and labelled *advisory — does not block*.
5. **Memory Inspector** — natural-language Q&A over the live cluster via the CockroachDB Cloud MCP Server, with the executed SQL displayed under every answer.

A persistent **Savings** strip shows: stages reused, stages recomputed, duplicate launches prevented, failures avoided, seconds saved (measured), and rate-based cost with the formula visible.

**Judge mode:** the demo URL loads read-only with a seeded, deterministic history plus a **Run the demo** button that executes the four scenarios live. No login. A `Reset demo` control restores the seed state.

---

## 8. Non-Goals

Cairn is not, and the code contains no path toward: compliance/audit/governance software, an approval-gate or policy engine, a certification or credentialing system, a CI/CD replacement, a generic observability dashboard, a chatbot, a RAG app, a documentation assistant, a code-review bot, a compiler, a static-analysis product, or a new language. The static analysis in Cairn exists solely to answer reachability for reuse decisions; it emits no diagnostics and no report.

Cairn also does not: replace your orchestrator (it plugs into Make/GitHub Actions/Dagster), manage secrets, schedule capacity, or claim to prove full artifact equivalence.

---

## 9. Requirement Compliance Matrix

| Requirement | Where satisfied | Evidence in demo/video |
|---|---|---|
| Genuinely agentic | §6.4 — nine actions, autonomous, memory-driven, approval only on cost/override/quarantine | Scenario 2: refuse → remediate → re-plan → launch |
| CockroachDB as load-bearing persistent memory | §6.1, §11 schema — claims, causal graph, negative memory, decisions, fragments | Every panel reads live from the cluster; Claim Theatre shows serializable arbitration |
| Deployed on AWS | §6.3 — ECS Fargate ×2 regions, S3, Bedrock, Lambda, EventBridge, CloudWatch, CloudFront | Public URL; ECS task ARNs shown in UI |
| ≥2 CockroachDB tools, meaningfully | §6.2 — all four, each tied to a visible behaviour | Vector search blocks a run; MCP answers a judge's question; `ccloud` provisions in the setup clip; skills documented |
| ≥1 AWS service, meaningfully | §6.3 — six | Bedrock classification shown inline; S3 URIs shown per artifact |
| Functioning public demo | §7.2 | The URL itself |
| Public OSS repo, Apache-2.0 | Repo root | `LICENSE`, `README.md` |
| Clear setup/run instructions | `README.md` — 6 commands to a running system | Quickstart block |
| Video < 3 min | §12 script — 2:55 | — |
| CockroachDB memory visibly working | Claim Theatre + Decision Ledger + Memory Inspector | Live SQL and live rows on screen |
| Explain what each tool does | §6.2, §6.3, `docs/TOOLS.md` | Video 2:25–2:45 |
| New project in submission period | First commit ≥ 2026-08-06, no vendored prior work | `git log` |
| No simulation | §5, §13 | Real tracebacks, real S3 objects, real ECS ARNs, real durations |

---

## 10. What the CockroachDB Skills Changed

Recorded honestly, because "we installed it" is not meaningful use.

1. **Retry scope.** The initial `with_retry` wrapper retried only the failing statement. The transaction/resilience skill's guidance made clear that under `SERIALIZABLE` the *entire* transaction must be replayed on `40001`. `cairn/db/txn.py` was rewritten to take a closure and replay it whole. This is the single change that makes the claim protocol correct under contention.
2. **`SELECT ... FOR UPDATE` on the contended read.** The claim-contention path originally did a plain `SELECT` after a failed insert, which produced avoidable retry churn under the two-region race. Locking the row early cut observed `40001` retries in the race test from a mean of 3.1 to 0.4 per contended claim.
3. **Vector index prefix columns.** The performance skill plus the vector-index docs established that filtered vector search is only accelerated when filters match prefix columns; the index was redefined as `(stage, error_class, embedding vector_cosine_ops)` rather than `(embedding)` alone.
4. **Batch-insert avoidance.** Vector inserts are written one row at a time, per documented guidance that large batch inserts of `VECTOR` degrade performance.

`docs/SKILLS_USAGE.md` carries these with file/line references and the before/after benchmark numbers.

---

## 11. Data Model (authoritative)

```sql
-- ============ identity & environment ============
CREATE TABLE environments (
  env_fingerprint   STRING PRIMARY KEY,
  image_digest      STRING NOT NULL,
  python_version    STRING NOT NULL,
  deps              JSONB  NOT NULL,       -- sorted resolved dependency set
  torch_threads     INT    NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ causal artifact graph ============
CREATE TABLE artifacts (
  artifact_id       STRING PRIMARY KEY,     -- content address (sha256 of payload)
  stage             STRING NOT NULL,
  work_key          STRING NOT NULL,
  s3_uri            STRING NOT NULL,
  size_bytes        INT8   NOT NULL,
  env_fingerprint   STRING NOT NULL REFERENCES environments(env_fingerprint),
  produced_by_run   UUID   NOT NULL,
  duration_ms       INT8   NOT NULL,
  vcpu              DECIMAL NOT NULL,
  mem_mib           INT    NOT NULL,
  region            STRING NOT NULL,
  quarantined_at    TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (work_key, created_at DESC),
  INDEX (stage, created_at DESC)
);

CREATE TABLE artifact_inputs (
  artifact_id       STRING NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
  input_kind        STRING NOT NULL,        -- code|config|data|upstream|env
  input_ref         STRING NOT NULL,        -- symbol id | config key path | partition id | artifact_id
  input_digest      STRING NOT NULL,
  PRIMARY KEY (artifact_id, input_kind, input_ref)
);

CREATE TABLE code_units (
  unit_id           STRING PRIMARY KEY,     -- module:qualname
  module            STRING NOT NULL,
  qualname          STRING NOT NULL,
  ast_digest        STRING NOT NULL,        -- digest of canonicalized AST
  is_private        BOOL   NOT NULL,
  commit_sha        STRING NOT NULL,
  INDEX (commit_sha, module)
);

CREATE TABLE code_edges (
  commit_sha  STRING NOT NULL,
  src_unit    STRING NOT NULL,
  dst_unit    STRING NOT NULL,
  edge_kind   STRING NOT NULL,             -- calls|imports|reads_global
  PRIMARY KEY (commit_sha, src_unit, dst_unit, edge_kind)
);

-- ============ distributed claims ============
CREATE TABLE work_claims (
  work_key          STRING PRIMARY KEY,
  stage             STRING NOT NULL,
  state             STRING NOT NULL,        -- CLAIMED|RUNNING|SUCCEEDED|FAILED|ABANDONED
  owner_id          STRING NOT NULL,
  owner_host        STRING NOT NULL,
  owner_region      STRING NOT NULL,
  fence             INT8   NOT NULL DEFAULT 1,
  lease_expires_at  TIMESTAMPTZ NOT NULL,
  cancel_requested  BOOL   NOT NULL DEFAULT false,
  run_id            UUID   NOT NULL,
  artifact_id       STRING,
  claimed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (state, lease_expires_at),
  CHECK (state <> 'SUCCEEDED' OR artifact_id IS NOT NULL)
);

CREATE TABLE ownership_transfers (
  transfer_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  work_key     STRING NOT NULL,
  from_owner   STRING NOT NULL,
  to_owner     STRING NOT NULL,
  from_fence   INT8   NOT NULL,
  to_fence     INT8   NOT NULL,
  reason       STRING NOT NULL,             -- lease_expired|cancelled
  at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ runs & durable fragments ============
CREATE TABLE runs (
  run_id       UUID PRIMARY KEY,
  work_key     STRING NOT NULL,
  stage        STRING NOT NULL,
  state        STRING NOT NULL,             -- RUNNING|SUCCEEDED|FAILED|DISPOSSESSED
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at     TIMESTAMPTZ,
  region       STRING NOT NULL,
  task_arn     STRING,
  INDEX (work_key, started_at DESC)
);

CREATE TABLE run_fragments (
  work_key       STRING NOT NULL,
  fragment_index INT    NOT NULL,
  run_id         UUID   NOT NULL,
  fence          INT8   NOT NULL,
  s3_uri         STRING NOT NULL,
  content_digest STRING NOT NULL,
  duration_ms    INT8   NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (work_key, fragment_index)
);

-- ============ negative memory ============
CREATE TABLE failure_signatures (
  signature_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  stage             STRING NOT NULL,
  error_class       STRING NOT NULL,
  workload_kind     STRING NOT NULL,
  model_family      STRING, model_id STRING,
  embedding_dim     INT, num_labels INT, dataset_rows INT8,
  max_seq_len       INT, batch_size INT, grad_accum INT,
  precision         STRING, optimizer STRING, lr DECIMAL,
  instance_kind     STRING, vcpu DECIMAL, mem_mib INT, accelerator STRING,
  framework         STRING, framework_version STRING,
  error_module      STRING, exit_code INT, oom_killed BOOL NOT NULL DEFAULT false,
  traceback_head    STRING NOT NULL,
  summary_text      STRING NOT NULL,
  embedding         VECTOR(1024) NOT NULL,
  run_id            UUID NOT NULL,
  wasted_ms         INT8 NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (stage, error_class, created_at DESC)
);

SET CLUSTER SETTING feature.vector_index.enabled = true;   -- see §13.2 fallback
CREATE VECTOR INDEX fs_sem
  ON failure_signatures (stage, error_class, embedding vector_cosine_ops);

CREATE TABLE remediations (
  remediation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  signature_id    UUID NOT NULL REFERENCES failure_signatures(signature_id),
  changed_keys    JSONB NOT NULL,           -- [{key, from, to}]
  rationale       STRING NOT NULL,
  verified_run_id UUID,                     -- set when a run using it succeeded
  succeeded       BOOL NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (signature_id, succeeded)
);

-- ============ decisions, probes, contradictions ============
CREATE TABLE reuse_decisions (
  decision_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  work_key        STRING NOT NULL,
  stage           STRING NOT NULL,
  action          STRING NOT NULL,          -- the nine actions
  verdict         STRING NOT NULL,          -- reuse|recompute|refused|subscribed|resumed
  change_class    STRING,                   -- comment_only|logging_only|...
  proposed_by     STRING NOT NULL,          -- rule|model
  model_id        STRING,
  authorized_by   STRING,                   -- probe|structural|identity  (NEVER 'model')
  probe_run_id    UUID,
  candidate_artifact_id STRING,
  latency_ms      INT8 NOT NULL,
  explanation     STRING NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (created_at DESC),
  CHECK (verdict <> 'reuse' OR authorized_by IN ('probe','structural','identity'))
);

CREATE TABLE probe_runs (
  probe_run_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  probe_type      STRING NOT NULL,          -- P1..P6
  artifact_id     STRING,
  sample_spec     STRING NOT NULL,          -- exact deterministic selection rule
  population_size INT8, sample_size INT8,
  tolerance       STRING NOT NULL,          -- 'bitwise' in conservative mode
  runtime_ms      INT8 NOT NULL,
  passed          BOOL NOT NULL,
  evidence_digest STRING NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE contradictions (
  contradiction_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id       STRING NOT NULL,
  contradicting_run UUID NOT NULL,
  evidence          STRING NOT NULL,
  quarantined       BOOL NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ transparent cost ============
CREATE TABLE cost_rates (
  resource_kind  STRING PRIMARY KEY,        -- fargate_vcpu_hour | fargate_gb_hour | ...
  usd            DECIMAL NOT NULL,
  source_note    STRING NOT NULL,           -- e.g. 'AWS published on-demand us-east-1, 2026-08'
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 12. Demo Script (2:55)

| Time | On screen | Proves |
|---|---|---|
| 0:00–0:15 | Terminal: `cairn run --all` cold. 138 s compressed with an on-screen real-time counter and a visible "recorded at 1×, playback 6×" label. Artifacts appear in S3; rows appear in CockroachDB live. | Real workload, real storage, no simulation |
| 0:15–0:45 | Add a docstring + `logger.debug` to `train.py`. `cairn run --all`. Console: five green nodes. Click `checkpoint`: class `logging_only`, probe `P4 checkpoint_logit`, 128/2400 sample, tolerance bitwise, 610 ms. **4 s total.** | Evidence-backed reuse; probe honesty on screen |
| 0:45–1:20 | Change `train.hidden_dim: 256 → 512`. `features` green (reused, 0 s), `checkpoint`+`eval` amber (recomputed, 32 s). Causal graph shows the `artifact_inputs` edges consulted. | Causal partial reuse — the expensive stage survives an architecture change |
| 1:20–2:00 | Split screen: worker A (us-east-1) and worker B (us-west-2) launched within 300 ms on the same key. Claim Theatre: A wins, fence=1; B subscribes and shows A's ECS task ARN and live progress; A is killed mid-run; reaper marks expiry; B takes over at fence=2, resumes from fragment 2/3; A revives, writes zero rows, logs `dispossessed`, exits. B returns the artifact. | Serializable duplicate prevention, fencing, crash resume, split-brain prevention — all on real CockroachDB |
| 2:00–2:25 | Set `train.classifier.input_dim` wrong for `mpnet`. `cairn plan` refuses **before launching**: `strong_semantic`, cosine 0.11, the remembered traceback, the remediation. Apply it, re-plan (key recomputed, memory re-queried), launch. Savings strip ticks: failures avoided +1. | Negative memory with tiering; refuse → remediate → re-evaluate → launch |
| 2:25–2:45 | Memory Inspector: judge-style question answered via the **CockroachDB Cloud MCP Server**, with the executed SQL shown. Quick cut of `ccloud cluster list` and the skills directory. | All four CockroachDB tools, visibly |
| 2:45–2:55 | Savings strip: reused/recomputed, duplicates prevented, failures avoided, **measured** seconds saved, rate-based cost with the formula visible. | Measurement honesty |

---

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Vector index unavailable on the chosen CockroachDB Cloud plan (`feature.vector_index.enabled` not settable) | Target **CockroachDB Cloud Standard** (30-day trial). `cairn doctor` detects index availability and falls back to exact brute-force cosine via `<=>` with no index — correct, just slower, and fine at demo cardinality. The fallback is tested in CI. |
| Bedrock model access not enabled in the account/region | `cairn doctor` checks model access at setup and fails loudly with the exact console link. `--no-llm` runs the deterministic-only path: `exact` matches, structural classes, and all probes still work; only ambiguous-change classification and remediation authoring degrade to rules. **The demo's correctness path never depends on the LLM.** |
| ECS cold start eats the demo | Workers are pre-warmed; the video labels the one compressed segment explicitly. |
| Static reachability is unsound for dynamic Python | Hard refusal on `importlib`/`__import__`/`getattr`-dispatch/`eval`/`exec`/entry-points in the reachable set. Documented as an approximation with a stated restriction, not as a proof. |
| Probe passes but artifact is actually stale | Contradiction detection + one-way quarantine (§6.5), plus conservative-mode bitwise tolerance and hard refusal on env drift. |
| Two-region Fargate cost | Demo tasks are 2 vCPU / 4 GiB, minutes at a time; teardown script deletes the cluster and services. Estimated total spend for the full build and demo period is under $15, itemized in `docs/COST.md`. |
| Scope overrun before 2026-08-18 | §Cut list in `PLAN.md` — MCP inspector, second region, and the extrapolation panel are the ordered cuts. The core (claims + causal reuse + negative memory + probes) is D1–D6. |

---

## 14. Success Criteria

The build is done when, on a clean AWS account and a fresh cluster, `make demo` reproduces all five behaviours end-to-end with no manual steps, and:

- every number in the UI traces to a measured value or a visible formula;
- no `reuse_decisions` row exists with `verdict='reuse'` and `authorized_by='model'` (structurally impossible);
- the race test passes 200/200 iterations against real CockroachDB with exactly one winner and zero double-writes;
- the determinism test produces bit-identical artifacts across three runs;
- `cairn doctor` is green on all checks.
