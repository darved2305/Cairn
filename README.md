# Cairn

**Causal reuse memory for expensive compute.**

Cairn remembers what your compute already proved, refuses work that is already
running or already known to fail, and recomputes only what a change can
actually affect.

Built for the CockroachDB × AWS Hackathon. See [`PROJECT.md`](PROJECT.md) for
the full design and [`PLAN.md`](PLAN.md) for the build schedule.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

---

## The problem

Every build cache you already use — Make, Bazel, DVC, Metaflow, W&B artifacts,
Dagster asset caches, Docker layers — is a **declared-input hasher**. It keys on
the declared inputs and invalidates when the key changes. That is correct,
conservative, and lossy in one specific way:

> A change to a declared input is treated as proof of invalidation. It is not.
> It is only *evidence of possible* invalidation.

| Change | Your cache | Reality |
|---|---|---|
| Docstring added to `train.py` | Invalidates the checkpoint | Cannot affect the checkpoint |
| `logger.debug(...)` in the training loop | Invalidates the checkpoint | Logging has no return-value effect on the computation |
| `eval.py` rewritten | Invalidates the whole pipeline, if keyed on a tree hash | Cannot affect the feature table or checkpoint — they are upstream |
| `eval.batch_size` changed in `config.yaml` | Invalidates everything keyed on that file | Affects evaluation throughput only |

Cairn answers a different question — *can this change reach this artifact, and
if it can, does it alter it?* — using code structure, recorded causal edges, and
a cheap deterministic probe that recomputes a bounded sample and compares
canonical bytes.

**The safety rule, enforced by the schema and not by convention:**

> The model may propose reuse. Deterministic evidence must authorize it.

`reuse_decisions.authorized_by` is one of `probe`, `structural`, or `identity`.
There is no enum value for `model`, and a `CHECK` constraint makes a
model-authorized reuse unrepresentable.

---

## Quickstart

Six commands from a clean clone to a running system:

```bash
uv sync                                     # 1. install the pinned environment
./scripts/provision_cluster.sh              # 2. create the CockroachDB Cloud cluster, write .env
make migrate                                # 3. apply db/migrations/*.sql
make seed                                   # 4. seed negative memory with three REAL failures
make console-build && make console          # 5. build + serve the console on :8000
make demo                                   # 6. run the pipeline for real (cairn run --all)
```

`make check` (lint + typecheck + unit and property tests) needs no database.
Integration tests need a real cluster — `scripts/provision_cluster.sh` for
CockroachDB Cloud, or `./scripts/local_cluster.sh up` for a single node in
Docker. **No test in this repo mocks the database for anything claiming to be
an integration test**; see [`PLAN.md`](PLAN.md) §5.

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+ (console and
TUI only), and the [`ccloud`](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started)
CLI. AWS credentials with Bedrock model access are needed for embeddings and
the LLM paths — without them, `CAIRN_NO_LLM=1` runs the deterministic-only
path (see [Degradation](#degradation) below).

---

## What Cairn does

### 1. Causal partial reuse

Walks the five-stage DAG in topological order and classifies each node
independently against the recorded `artifact_inputs` edges of the previous
successful run.

```
env → dataset → features → checkpoint → eval
```

| Change | env | dataset | features | checkpoint | eval |
|---|---|---|---|---|---|
| Comment + `logger.debug` in `train.py` | reuse | reuse | reuse | **reuse** | reuse |
| `eval.metrics += ["macro_f1"]` | reuse | reuse | reuse | reuse | **recompute** |
| `train.hidden_dim: 256 → 512` | reuse | reuse | **reuse** | **recompute** | recompute |
| Embedding model → `all-mpnet-base-v2` | reuse | reuse | **recompute** | recompute | recompute |
| `pip install` bumps `torch` | **recompute** | reuse | recompute | recompute | recompute |

Row three is the point: the expensive feature stage survives an architecture
change, because the architecture is not in that stage's recorded read set.

### 2. Transactional duplicate-work prevention

Identical semantic work on any machine in any region produces an identical
`work_key`. A single `SERIALIZABLE` transaction acquires the claim; a monotonic
fence rides every subsequent write. A resurrected worker with a stale fence
updates zero rows, detects it, and exits without writing. The loser of a race
does not error — it subscribes, watches the winner's progress, and adopts its
artifact. There is no in-memory lock anywhere in Cairn.

### 3. Negative computational memory

Every failed run writes a structured feature vector plus a 1024-d Titan
embedding of a normalized failure summary. A new plan is checked against that
memory **before any claim is taken**, and a blocking match halts the plan and
proposes the remediation that actually worked.

| Tier | Condition | Action |
|---|---|---|
| `exact` | All causal structured features equal, same `framework_version`, same `instance_kind` | Refuse. Propose the recorded remediation. |
| `strong_semantic` | Cosine ≤ 0.15, same stage + error class, ≥ 1 causal feature matching | Refuse, present evidence, require a modified plan or explicit `--override` |
| `weak` | Cosine ≤ 0.35, no structured agreement beyond stage | **Advisory only. Never blocks.** Labelled as such next to every match. |

Vector similarity alone never gates execution. That is the whole design.

### 4. Evidence-backed reuse

Six probe types, each with a stated guarantee **and** an explicit
non-guarantee — see [`docs/PROBES.md`](docs/PROBES.md). Cairn never claims a
probe proves full artifact equivalence, and the UI renders sample/population as
a fraction, always.

### 5. Crash recovery

Work is decomposed into fragments (`features` by shard, `checkpoint` by epoch),
each written with its S3 URI and content hash in one fenced transaction. On
worker death the reaper marks the lease takeover-eligible, the next contender
bumps the fence, and the new owner validates each fragment against S3 and
resumes from `max(index) + 1`.

---

## Product surface

### CLI

```
cairn init                        # scaffold cairn.yaml, register stages
cairn plan [stage]                # decisions + evidence, no execution, exit 0/1
cairn run <stage> [--all]         # the agent loop
cairn explain <artifact_id>       # full provenance + decision chain
cairn memory search "<text>"      # query negative memory directly
cairn memory why-blocked          # explain the last refusal
cairn doctor                      # cluster (via ccloud), AWS, schema, index health
cairn unquarantine <id> --reason "<text>"
cairn claim-demo                  # drive a claim by hand, for the race demo
```

`cairn plan` exits non-zero on a `REFUSE_DOOMED`, which makes it usable as a CI
gate: the pipeline stops before spending money on a run memory says will fail.

### Console — the public demo URL

`src/cairn/console/` (FastAPI) + `console/frontend/` (React + Vite +
TypeScript + Tailwind), built into **one image serving one port**.

Five panels, all reading live from CockroachDB:

1. **Causal Graph** — the five-node DAG colour-coded by verdict; click any node
   for the class that applied, the probe that ran, its sample/population
   fraction, its runtime, and the `artifact_inputs` edges consulted.
2. **Decision Ledger** — append-only, with actor, authority, and latency.
3. **Claim Theatre** — live `work_claims`: owners, regions, fences, lease
   countdowns, fragment progress, and the ownership-transfer audit trail.
4. **Negative Memory** — searchable and tiered; weak matches are visually
   distinct and labelled *advisory — does not block*.
5. **Memory Inspector** — natural-language Q&A over the live cluster via the
   CockroachDB Cloud MCP Server, **with the executed SQL shown under every
   answer**.

Plus a persistent **Savings** strip and judge mode: read-only, no login, seeded
deterministic history on load, with **Run the demo** and **Reset demo**
controls.

```bash
make console-build     # npm ci && npm run build in console/frontend
make console           # uvicorn on :8000, serving API + SPA from one port
make console-check     # tsc --noEmit
```

For frontend development, run `make console` in one terminal and
`cd console/frontend && npm run dev` in another — Vite proxies `/api` to the
FastAPI process, so both modes are same-origin and no environment-specific base
URL is ever baked into the bundle.

### Interactive terminal

Running `cairn` with no subcommand and a real TTY launches an interactive TUI.
The Python side never renders it directly: it writes a versioned NDJSON event
stream at real state transitions (`src/cairn/obs/events.py`) which the TUI
tails, and the TUI drives real work by spawning `cairn <subcommand>` itself.

The TUI on this branch is the TypeScript implementation in `tui/`, built on
[`@earendil-works/pi-tui`](https://github.com/earendil-works/pi) (MIT — see
[`NOTICE`](NOTICE)). A Rust rewrite targeting a persistent multi-pane layout is
in progress on a separate branch and is **not** part of this one; nothing below
depends on it.

```bash
make tui         # cd tui && npm install && npm run build
cairn            # launches it
make tui-check   # tsc --noEmit
make tui-test    # unit / render-width tests
```

Requires Node ≥ 22.19. If `tui/dist/index.js` is unbuilt or `node` is missing,
`cairn` prints a clear error and exits — it never launches a broken TUI or
silently falls back.

---

## Deployment

Terraform in [`infra/`](infra) provisions ECR, S3, two-region ECS Fargate,
an ALB behind CloudFront, the Lambda reaper on a 30-second EventBridge
schedule, CloudWatch logs/metrics/alarms, and least-privilege IAM.

```bash
cd infra && terraform init && terraform plan     # review before applying
```

`terraform apply` creates resources that bill by the hour. Read
[`docs/COST.md`](docs/COST.md) first — it explains why there is no NAT Gateway
anywhere, and what `make teardown` removes.

Two deployment follow-ups are prepared but deliberately **not** applied, because
each is a real change to live infrastructure:

- **Read-only console role.** `db/migrations/0008_console_readonly_role.sql`
  creates a `SELECT`-only role; `scripts/provision_console_role.py` creates the
  login user, prints its connection URL, and *verifies* the result by
  reconnecting as that user and asserting a write is rejected. `infra/ecs.tf`
  carries the step-by-step block for wiring it as the console's own Secrets
  Manager secret. Until that runs, the console shares the workers' credential
  and read-only is enforced only by `console/queries.py` containing nothing but
  SELECTs.
- **Cost rates.** `db/migrations/0007_cost_rates_seed.sql` seeds published AWS
  Fargate on-demand rates. Until applied, `/api/savings` returns `cost: null`
  with the reason — which is the correct behaviour, not a bug.

---

## Degradation

Cairn's correctness path never depends on the LLM.

| Unavailable | What still works | What degrades |
|---|---|---|
| Bedrock entirely (`CAIRN_NO_LLM=1`) | `exact` matches, all structural classes, all six probes, the whole claim protocol | `strong_semantic`/`weak` matching (no embeddings); ambiguous-change classification and remediation authoring fall back to rules; the Memory Inspector reports itself unavailable rather than answering from the schema alone |
| Vector index | Everything | `search` falls back to exact brute-force cosine via `<=>` — correct, just slower |
| CockroachDB Cloud MCP key | Every panel, including the Inspector | The Inspector's four tools run over pgwire instead, and the response says so — an answer produced that way never claims to have come from the MCP server |

Every one of those degradations is surfaced in the UI with the real underlying
cause. A 503 from the Memory Inspector prints the actual IAM denial, because
"Bedrock model access is not enabled in this account" is worth far more than
"something went wrong".

---

## Measurement honesty

The UI shows only measured values, plus clearly-labelled arithmetic on them.

- **Measured** — stage and probe wall-clock, artifact bytes, S3 keys,
  vCPU/memory allocation, region, stages reused/recomputed, duplicate launches
  prevented, failures avoided, fragments resumed.
- **Derived, labelled `rate-based`** — cost, computed from the user-editable
  `cost_rates` table, rendering its own formula inline:
  `95.2 s × $0.0000274/s = $0.0026`.
- **Never shown** — an invented dollar figure presented as an observation.
  There is no code path that produces one. When no rate row exists, the API
  returns `cost: null` with the reason and the UI prints the reason.

---

## Repository layout

| Path | What's in it |
|---|---|
| `src/cairn/agent/` | The nine-action agent loop |
| `src/cairn/db/` | Claims, decisions, memory, graph, fragments, and the SERIALIZABLE retry wrapper |
| `src/cairn/fingerprint/` | AST canonicalization, reachability, work-key composition |
| `src/cairn/probes/` | P1–P6 |
| `src/cairn/workload/` | The real five-stage pipeline |
| `src/cairn/console/` | Read-only API, MCP/SQL tools, Memory Inspector agent, demo replay |
| `console/frontend/` | The React SPA |
| `db/migrations/` | Forward-only schema |
| `infra/` | Terraform |
| `tests/` | `unit/`, `property/` (Hypothesis), `integration/` (real cluster, no mocks) |
| `docs/` | [Architecture](docs/ARCHITECTURE.md) · [Tools](docs/TOOLS.md) · [Probes](docs/PROBES.md) · [Cost](docs/COST.md) · [Skills usage](docs/SKILLS_USAGE.md) |

## Non-goals

Cairn is not compliance software, a policy engine, a CI/CD replacement, a
generic observability dashboard, a chatbot, a RAG app, a code-review bot, or a
static-analysis product. The static analysis here exists solely to answer
reachability for reuse decisions; it emits no diagnostics and no report. Cairn
does not replace your orchestrator — it plugs into Make, GitHub Actions, or
Dagster — and it does not claim to prove full artifact equivalence.

## License

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
