# CAIRN — Implementation Plan

Companion to `docs/project/PROJECT.md`. That document decides *what* and *why*; this one decides *how*, *in what order*, and *when to stop*.

**Calendar:** today is **2026-08-06**. Hard deadline **2026-08-18 17:00 EDT**. That is 12 working days with a mandatory 1-day buffer. Day 12 is submission, not development.

**Non-negotiable ordering principle:** the correctness core (claims → causal graph → probes → negative memory) is built and tested against real CockroachDB before any UI work begins. A beautiful console over a broken claim protocol scores zero on two of the five criteria.

---

## 1. Repository Layout

```
cairn/
├── LICENSE                       # Apache-2.0
├── README.md                     # 6-command quickstart, tool explanations, screenshots
├── docs/project/PROJECT.md
├── docs/project/PLAN.md
├── Makefile                      # cluster, migrate, seed, demo, race, teardown
├── cairn.yaml                    # stage registry + config for the demo pipeline
├── pyproject.toml                # uv/pip, pinned
├── requirements.lock             # fully resolved, hashed
│
├── .agents/skills/               # cockroachdb-skills (committed, Apache-2.0)
├── .github/workflows/
│   ├── ci.yml                    # lint, unit, integration on ephemeral ccloud cluster
│   └── demo-deploy.yml           # build + push image, update ECS services
│
├── db/migrations/                # 0001_init.sql .. 0009_cost_rates.sql (forward-only)
│
├── src/cairn/
│   ├── cli.py                    # typer: init/plan/run/explain/memory/doctor/unquarantine
│   ├── config.py                 # instrumented config accessor (records read keys)
│   ├── db/
│   │   ├── pool.py               # psycopg3 pool, TLS, app_name
│   │   ├── txn.py                # SERIALIZABLE retry wrapper  ★ correctness-critical
│   │   ├── claims.py             # acquire / heartbeat / complete / takeover / subscribe ★
│   │   ├── graph.py              # artifacts + artifact_inputs read/write
│   │   ├── memory.py             # failure signatures, tiering, vector search ★
│   │   └── decisions.py          # reuse_decisions, probe_runs, contradictions
│   ├── fingerprint/
│   │   ├── canon.py              # canonical JSON, canonical float32 bytes
│   │   ├── env.py                # env fingerprint
│   │   ├── astcanon.py           # AST canonicalization + digest ★
│   │   ├── reach.py              # import/call graph + reachability ★
│   │   └── workkey.py            # work_key composition ★
│   ├── classify/
│   │   ├── rules.py              # deterministic change classes + refusal guards ★
│   │   └── llm.py                # Bedrock Claude, structured output, proposal only
│   ├── probes/
│   │   ├── base.py               # Probe protocol + ProbeResult
│   │   ├── p1_env.py  p2_struct.py  p3_features.py
│   │   ├── p4_logits.py  p5_schema.py  p6_evalslice.py
│   ├── agent/
│   │   ├── loop.py               # perceive → recall → decide → act → learn ★
│   │   └── actions.py            # the nine actions
│   ├── workload/                 # THE REAL PIPELINE
│   │   ├── stage_env.py  stage_dataset.py  stage_features.py
│   │   ├── stage_train.py  stage_eval.py
│   │   └── determinism.py        # seeds, thread pinning, rlimit
│   ├── storage/s3.py             # content-addressed put/get, fragment IO
│   ├── obs/                      # structured logs, CloudWatch EMF metrics
│   └── console/                  # FastAPI + React (Vite) SPA
│
├── infra/                        # Terraform
│   ├── ecs.tf  s3.tf  lambda.tf  events.tf  iam.tf  alb.tf  cloudfront.tf
│   └── vars.tf
├── lambda/reaper/handler.py
├── scripts/
│   ├── provision_cluster.sh      # ccloud create + migrate + role
│   ├── seed_memory.py            # produce REAL F1/F2/F3 failures, record them
│   ├── race.py                   # two-region duplicate race driver
│   └── kill_worker.sh            # crash-recovery driver
└── tests/
    ├── unit/                     # fingerprints, classes, canonicalization
    ├── integration/              # real cluster: claims, race, takeover, vector
    └── property/                 # hypothesis: work_key stability, fence monotonicity
```

---

## 2. Environment Decisions (locked, do not relitigate)

| Decision | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | The workload is Python; AST analysis needs the same runtime |
| DB driver | `psycopg[binary,pool] 3.2` | Mature `SERIALIZABLE` + explicit retry control |
| CockroachDB plan | **Cloud Standard** (30-day trial), `us-east-1` primary | Vector index availability; Basic is the tested fallback |
| Isolation | `SERIALIZABLE` (CockroachDB default) | Claim correctness depends on it; never downgrade |
| LLM | Bedrock `anthropic.claude-sonnet-5` via `AnthropicBedrockMantle` | Messages-API parity; structured outputs supported on Bedrock |
| Embeddings | `amazon.titan-embed-text-v2:0`, `dimensions=1024`, `normalize=true` | Normalized vectors + `vector_cosine_ops` |
| Compute | ECS Fargate 2 vCPU / 4 GiB, `us-east-1` + `us-west-2` | Real cross-region race, no GPU quota risk |
| Console | FastAPI + React (Vite), single container | One image, one deploy path |
| IaC | Terraform | Reviewable, teardown-able |
| Package manager | `uv` | Lock reproducibility feeds the env fingerprint |

**Bedrock call shape** (locked — matches current API):

```python
from anthropic import AnthropicBedrockMantle
client = AnthropicBedrockMantle(aws_region="us-east-1")

resp = client.messages.create(
    model="anthropic.claude-sonnet-5",
    max_tokens=4096,
    thinking={"type": "adaptive"},
    output_config={
        "effort": "medium",
        "format": {"type": "json_schema", "schema": CHANGE_CLASSIFICATION_SCHEMA},
    },
    system=[{"type": "text", "text": CLASSIFIER_SYSTEM,
             "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": diff_payload}],
)
```

Notes that will otherwise cost an afternoon:
- **No `temperature`/`top_p`/`top_k`** on Sonnet 5 — non-default values are rejected.
- **No `budget_tokens`** — use `output_config.effort`.
- **No assistant prefill** — use `output_config.format`.
- Bedrock model IDs carry the `anthropic.` prefix; the first-party ID will 400.
- Structured outputs are supported on Bedrock; Message Batches and the Files API are not — we use neither.
- Prompt caching: system prompt is frozen and cached; the diff goes after the breakpoint.

---

## 3. The Four Correctness-Critical Modules

These are written first, tested hardest, and must not be improvised at 2 a.m. on day 10.

### 3.1 `db/txn.py` — serializable retry

```python
import random, time
import psycopg
from psycopg.errors import SerializationFailure

MAX_ATTEMPTS = 8
BASE_MS = 50
CAP_MS = 2000

def in_txn(pool, fn, *, op: str):
    """Run fn(cur) inside one SERIALIZABLE transaction, replaying the WHOLE
    closure on SQLSTATE 40001. fn MUST be a pure function of its arguments."""
    last = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with pool.connection() as conn:
                conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
            if attempt:
                METRICS.count("TxnRetries40001", attempt, op=op)
            return result
        except SerializationFailure as e:          # 40001
            last = e
            backoff = min(CAP_MS, BASE_MS * (2 ** attempt))
            time.sleep((backoff * random.uniform(0.75, 1.25)) / 1000.0)
    raise RuntimeError(f"txn '{op}' exhausted {MAX_ATTEMPTS} retries") from last
```

**Rules enforced by review:** no I/O inside `fn` (no S3, no Bedrock, no `print`), no reliance on values computed in a previous attempt, no partial state written outside the transaction.

### 3.2 `db/claims.py` — the claim protocol

```python
LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 10

def acquire(pool, work_key, stage, owner, host, region, run_id) -> Claim:
    def _tx(cur):
        cur.execute("""
            INSERT INTO work_claims
              (work_key, stage, state, owner_id, owner_host, owner_region,
               fence, lease_expires_at, run_id)
            VALUES (%s,%s,'CLAIMED',%s,%s,%s,1, now() + %s, %s)
            ON CONFLICT (work_key) DO NOTHING
            RETURNING fence
        """, (work_key, stage, owner, host, region,
              f"{LEASE_SECONDS} seconds", run_id))
        row = cur.fetchone()
        if row:
            return Claim(won=True, fence=row[0])

        # Contended: lock the row before deciding. (Skill-driven change #2.)
        cur.execute("""
            SELECT state, owner_id, owner_host, owner_region, fence,
                   lease_expires_at, artifact_id, run_id
            FROM work_claims WHERE work_key=%s FOR UPDATE
        """, (work_key,))
        st, o, oh, orr, fence, exp, art, rid = cur.fetchone()

        if st == 'SUCCEEDED':
            return Claim(won=False, reuse_artifact_id=art)
        if st in ('FAILED', 'ABANDONED') or exp < _now(cur):
            cur.execute("""
                UPDATE work_claims
                   SET state='CLAIMED', owner_id=%s, owner_host=%s, owner_region=%s,
                       fence=fence+1, lease_expires_at=now() + %s,
                       run_id=%s, updated_at=now()
                 WHERE work_key=%s
             RETURNING fence
            """, (owner, host, region, f"{LEASE_SECONDS} seconds", run_id, work_key))
            new_fence = cur.fetchone()[0]
            cur.execute("""INSERT INTO ownership_transfers
                (work_key, from_owner, to_owner, from_fence, to_fence, reason)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (work_key, o, owner, fence, new_fence,
                 'lease_expired' if exp < _now(cur) else 'prior_terminal'))
            return Claim(won=True, fence=new_fence, took_over_from=o)

        return Claim(won=False, owner=o, owner_host=oh, owner_region=orr,
                     owner_fence=fence, run_id=rid)   # → SUBSCRIBE
    return in_txn(pool, _tx, op="claim.acquire")
```

Every later write is fenced. The heartbeat is the canary:

```python
def heartbeat(pool, work_key, owner, fence) -> bool:
    def _tx(cur):
        cur.execute("""UPDATE work_claims
                          SET lease_expires_at = now() + %s, updated_at = now()
                        WHERE work_key=%s AND owner_id=%s AND fence=%s
                          AND state IN ('CLAIMED','RUNNING')""",
                    (f"{LEASE_SECONDS} seconds", work_key, owner, fence))
        return cur.rowcount == 1
    return in_txn(pool, _tx, op="claim.heartbeat")
```

`heartbeat() == False` ⇒ **dispossessed** ⇒ log, mark the run `DISPOSSESSED`, exit immediately. This single check is what prevents split-brain writes.

Completion is one transaction that inserts the artifact and flips the claim together — never two.

### 3.3 `fingerprint/astcanon.py` + `reach.py`

`astcanon.canon_digest(path) -> str`:
1. `ast.parse(src)`
2. Strip docstrings (recorded separately as `docstring_digest`)
3. Zero out `lineno`/`col_offset`/`end_*`
4. `ast.unparse` the normalized tree
5. `sha256` of the result

Two files with the same digest are semantically identical modulo comments/formatting/docstrings. That is the whole engine behind `comment_only` and `formatting_only`.

`reach.reachable(entrypoint, commit_sha) -> set[unit_id]`:
- Build module graph from `import`/`from` nodes, restricted to first-party packages.
- Build call/reference edges from `ast.Call` + `ast.Name`/`ast.Attribute` loads, resolved with a simple scope walker (module globals → class → function locals).
- BFS from the entrypoint.
- **Escape hatch scan** — if any of `importlib`, `__import__`, `getattr`, `setattr`, `globals`, `vars`, `eval`, `exec`, `pkg_resources`, `entry_points` appears in the reachable set, return `UNSOUND` and **every structural class refuses**.

Unsoundness is a hard failure, not a warning. That is the honesty guarantee from `docs/project/PROJECT.md` §4.4/P2.

### 3.4 `db/memory.py` — tiered negative memory

```python
def search(pool, plan_features, embedding) -> list[Match]:
    def _tx(cur):
        cur.execute("""
            SELECT signature_id, stage, error_class, traceback_head, summary_text,
                   model_id, embedding_dim, num_labels, batch_size, max_seq_len,
                   wasted_ms, created_at,
                   embedding <=> %s AS dist
              FROM failure_signatures
             WHERE stage = %s
               AND (%s IS NULL OR error_class = %s)
             ORDER BY embedding <=> %s
             LIMIT 8
        """, (embedding, plan_features.stage,
              plan_features.error_class_hint, plan_features.error_class_hint,
              embedding))
        return cur.fetchall()
    rows = in_txn(pool, _tx, op="memory.search")
    return [tier(r, plan_features) for r in rows]
```

`tier()` implements the table in `docs/project/PROJECT.md` §4.1 exactly. Thresholds live in one module-level dict so they are tunable and testable:

```python
TIERS = {"exact_requires_structured": True,
         "strong_max_cosine": 0.15,
         "weak_max_cosine": 0.35}
```

Unit tests assert that a `weak` match **never** produces a blocking action, and a property test asserts `tier` is monotone in cosine distance.

**Vector index fallback:** the same SQL runs with or without the index. `cairn doctor` reports which path is active; CI runs the integration suite in both modes.

---

## 4. Day-by-Day Plan

Each day ends with a green `make check` and a commit. Days are ~8 hours.

### D1 — Wed 2026-08-06 · Foundations
- Repo, Apache-2.0 `LICENSE`, `pyproject.toml`, `uv` lock, pre-commit (ruff + mypy).
- `npx skills add cockroachlabs/cockroachdb-skills` → commit `.agents/skills/`; start `docs/project/SKILLS_USAGE.md`.
- `scripts/provision_cluster.sh`: `ccloud auth login`, `ccloud cluster create cairn-dev --plan standard`, `ccloud cluster user create`, capture connection URL into `.env`.
- `db/migrations/0001_init.sql` for environments/artifacts/artifact_inputs/work_claims/runs.
- `db/pool.py`, `db/txn.py` + unit tests with an injected `SerializationFailure` to prove whole-closure replay.
- **Exit:** `make cluster && make migrate` works from clean; `pytest tests/unit/test_txn.py` green.

### D2 — Thu 08-07 · Claims, proven
- `db/claims.py` complete: acquire / heartbeat / complete / fail / takeover / subscribe.
- `ownership_transfers`, fencing on every write.
- `tests/integration/test_claims.py` against the real cluster:
  - 2 workers, same key, 200 iterations → exactly one winner, every time;
  - loser subscribes and receives the artifact id;
  - dispossessed worker's fenced write affects 0 rows;
  - takeover only after expiry; fence strictly monotone (property test).
- **Exit:** `make race` passes 200/200 locally against CockroachDB Cloud.

### D3 — Fri 08-08 · The real workload
- `workload/` five stages, real, deterministic (`determinism.py`: seeds, `OMP_NUM_THREADS=1`, `use_deterministic_algorithms(True)`, `PYTHONHASHSEED=0`, `RLIMIT_AS`).
- Vendor the 20 Newsgroups 4-category snapshot and `all-MiniLM-L6-v2` into S3 / the image; write `data/DATASET.md`.
- `storage/s3.py` content-addressed put/get + fragment IO.
- Fragmentation: `features` → 3 shards; `checkpoint` → per-epoch.
- **Exit:** full pipeline runs locally in Docker end-to-end; determinism test produces bit-identical artifacts across 3 runs.

### D4 — Sat 08-09 · Causal graph + work keys
- `config.py` instrumented accessor that records which config keys each stage actually reads.
- `fingerprint/astcanon.py`, `reach.py`, `workkey.py`; `code_units` + `code_edges` population.
- `db/graph.py`: write `artifacts` + typed `artifact_inputs` on every successful stage.
- **Exit:** `cairn plan` prints per-stage work keys; changing `eval.metrics` changes only `eval`'s key. Property test: work key is invariant under comment/format edits and variant under semantic edits.

### D5 — Sun 08-10 · Change classification + probes
- `classify/rules.py`: the six classes with their refusal guards (dynamic-dispatch scan, `__doc__` usage scan, thread/process spawn scan).
- All six probes with `sample_spec`, `population_size`, `sample_size`, `tolerance='bitwise'`.
- `db/decisions.py` with the `CHECK` constraint migration; a test asserts the DB **rejects** `verdict='reuse', authorized_by='model'`.
- **Exit:** comment-only change → 5 nodes reused in ~4 s; `hidden_dim` change → features reused, checkpoint+eval recomputed.

### D6 — Mon 08-11 · Negative memory
- `scripts/seed_memory.py` runs F1/F2/F3 for real and records genuine tracebacks.
- Titan embeddings; `failure_signatures` + vector index migration; index-availability detection + brute-force fallback.
- Tiering, refusal path, remediation authoring (Bedrock, structured output), **re-plan loop** (recompute key → re-query memory → then claim).
- **Exit:** a never-seen `mpnet` misconfiguration is blocked at `strong_semantic` before any compute; applying the remediation lets it through.

> **Correctness core complete at end of D6.** Everything after this is deployment, surface, and polish. If D1–D6 slip, cut from §6, do not cut from here.

### D7 — Tue 08-12 · AWS deployment
- Terraform: S3, ECR, ECS cluster + task defs in `us-east-1` and `us-west-2`, ALB, CloudFront, IAM (least privilege: S3 prefix-scoped, `bedrock:InvokeModel` on two model ARNs only), Secrets Manager for the DB URL.
- Reaper Lambda + EventBridge 30 s.
- CloudWatch EMF metrics + 4 alarms (`ReuseRate` drop, `TxnRetries40001` spike, reaper failure, quarantine event).
- **Exit:** `make deploy` puts workers in both regions; `scripts/race.py` runs the cross-region race for real; `scripts/kill_worker.sh` triggers a real takeover.

### D8 — Wed 08-13 · Console I
- FastAPI read API over the live cluster; React shell.
- Causal Graph + Decision Ledger panels with evidence drill-down (class, probe, sample/population, runtime, edges).
- **Exit:** demo URL live behind CloudFront, read-only, no login.

### D9 — Thu 08-14 · Console II + MCP
- Claim Theatre (live claims, fences, subscription progress, ECS ARNs).
- Negative Memory panel with tier badges and the *advisory — does not block* label on weak matches.
- **Memory Inspector**: Bedrock agent over the CockroachDB Cloud MCP Server, read-only service account, executed SQL displayed. Respect the 20 s / 10 KiB / 25-row limits.
- Savings strip with formulas inline; extrapolation panel collapsed and labelled.
- **Exit:** a judge can ask a question and see real SQL + real rows.

### D10 — Fri 08-15 · CI, hardening, honesty pass
- `ci.yml`: ephemeral per-PR cluster via `ccloud` + service-account key; migrations; unit + integration (both vector modes); race test; determinism test; teardown.
- Full audit against `docs/project/PROJECT.md` §9 and the no-simulation rules: grep for `sleep(`, `random.` outside jitter/sampling, hardcoded dollar values, mocked DB in anything that claims to be integration.
- Security: no secrets in the image, read-only console role, S3 bucket block-public-access, CloudFront-only ALB ingress.
- **Exit:** CI green on a fresh PR; audit checklist signed off.

### D11 — Sat 08-16 · Record the video
- Rehearse the 6 beats; pre-warm tasks; reset seed state.
- Record 4K, one take per beat, label the single compressed segment "recorded at 1×, playback 6×".
- Edit to **≤ 2:55**; captions for every tool name; upload to YouTube (public).
- **Exit:** final video file + URL.

### D12 — Sun 08-17 · Docs + dry run
- `README.md` quickstart (6 commands), `docs/architecture/SUBSTRATES.md` (what each CockroachDB and AWS tool does), `docs/architecture/OVERVIEW.md` + diagram, `docs/internals/PROBES.md` (guarantees and non-guarantees), `docs/project/SKILLS_USAGE.md`, `docs/operations/COST.md`.
- Full clean-account dry run from `git clone` to working demo, timed, following only the README.
- Draft the Devpost submission.
- **Exit:** a stranger can reproduce the system from the README alone.

### D13 — Mon 08-18 · Submit by 12:00 EDT
Five hours of buffer before the 17:00 deadline. Submit early; do not touch `main` after submission.

---

## 5. Testing Strategy

| Layer | What it covers | Gate |
|---|---|---|
| **Unit** | AST canonicalization, reachability + escape-hatch detection, work-key composition, tiering, canonical float32 encoding, cost arithmetic | 100% on `fingerprint/`, `classify/`, `db/memory.py::tier` |
| **Property** (Hypothesis) | work key invariant under comment/format edits; variant under semantic edits; fence strictly increasing; `tier` monotone in distance; canonical JSON stable under key permutation | must pass 500 examples |
| **Integration** (real cluster, no mocks) | claim race ×200, takeover after expiry, dispossessed write = 0 rows, subscribe-and-adopt, fragment resume, vector search with **and** without index, `CHECK` rejects model-authorized reuse | required for merge |
| **Determinism** | 3 consecutive runs of `features` + `checkpoint` in the same image → identical digests | required for merge |
| **End-to-end** | `make demo` runs all four scenarios and asserts the expected verdict per node | required before video |
| **Chaos** | kill worker mid-fragment; sever DB connectivity mid-run; expire lease under load | manual, D7 and D10 |

**Anti-simulation lint** (`scripts/audit_no_sim.py`, runs in CI): fails the build on `time.sleep(` outside retry jitter and UI polling, on any literal `$` figure in `console/`, on `random` outside seeded sampling, and on any mocked DB in `tests/integration/`.

---

## 6. Cut List (ordered — cut from the top when behind)

1. **Extrapolation panel** — delete entirely. It is the least load-bearing and the highest-risk-of-looking-dishonest.
2. **Second region** — collapse the race to two ECS tasks in one region. Still a real distributed race across tasks; costs one sentence of framing.
3. **Memory Inspector (MCP)** — replaced by three canned-but-real SQL views in the console. *This drops a CockroachDB tool from 4 to 3; still above the minimum of 2.*
4. **P5/P6 probes** — keep P1–P4; `eval` reuse falls back to structural-only.
5. **Console polish** — ship the functional-but-plain version.

**Never cut:** claim protocol, fencing, fragment resume, negative memory tiering, probe honesty labels, conservative mode, the `CHECK` constraint.

---

## 7. Submission Checklist

- [ ] Public repo, Apache-2.0, first commit ≥ 2026-08-06, no pre-existing code
- [ ] `README.md`: quickstart, architecture diagram, tool explanations, screenshots
- [ ] Public demo URL live, read-only, no login, `Run the demo` + `Reset demo`
- [ ] Video ≤ 2:55, YouTube public, shows CockroachDB memory working
- [ ] `docs/architecture/SUBSTRATES.md` explains all four CockroachDB tools and all six AWS services
- [ ] `docs/internals/PROBES.md` states each probe's guarantee **and** non-guarantee
- [ ] `docs/project/SKILLS_USAGE.md` names the skills and the concrete changes they caused
- [ ] `docs/operations/COST.md` itemizes real spend and shows the rate table
- [ ] Architecture diagram attached to the Devpost entry
- [ ] Feedback on CockroachDB tools written (optional field — fill it; it is free signal to judges)
- [ ] CI green; race test 200/200; determinism test passing
- [ ] Clean-account dry run completed from README only
- [ ] Teardown script verified (`make teardown` deletes cluster, ECS, S3 objects)
- [ ] Submitted by **2026-08-18 12:00 EDT**

---

## 8. Open Decisions to Close on D1

1. **Cluster plan** — confirm Standard trial gives `SET CLUSTER SETTING feature.vector_index.enabled = true`. If not, switch primary to a self-hosted single node on ECS for dev and keep Cloud Standard for the demo cluster; the fallback path must still be tested.
2. **Bedrock region + model access** — enable `anthropic.claude-sonnet-5` and `amazon.titan-embed-text-v2:0` in `us-east-1` on day one; access approval is the only step with a queue.
3. **`CAIRN_APPROVAL_USD`** — demo value 0.50 so the escalation path is reachable on a real run rather than theoretical.
4. **Console auth** — none for judge mode; write mutations disabled at the IAM/role layer, not just in the UI.

---

## 9. Definition of Done

`make demo` on a clean AWS account and a fresh cluster reproduces all five headline behaviours with no manual steps, and:

- every number in the UI is measured or shown with its formula;
- `SELECT count(*) FROM reuse_decisions WHERE verdict='reuse' AND authorized_by='model'` returns 0 — and cannot return anything else, because the `CHECK` constraint forbids it;
- the race test passes 200/200 against real CockroachDB with exactly one winner and zero double-writes;
- three consecutive runs produce bit-identical artifacts;
- `cairn doctor` is green on every check.
