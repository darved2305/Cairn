# Tools: what each one does, and why it is load-bearing

Cairn uses **all four** CockroachDB tools and **six** AWS services. This
document explains what each does *in this system specifically* — not what it
does in general — and, for each, the visible behaviour that would disappear if
it were removed. "We installed it" is not meaningful use, so the test applied
throughout is: **what breaks without this?**

Where something is implemented but not yet verified end-to-end, that is stated
in the entry rather than left for a reader to discover.

---

# CockroachDB — 4 of 4 tools

## 1. Distributed Vector Indexing (C-SPANN)

**What it does here.** `failure_signatures.embedding` is a `VECTOR(1024)`
column holding an Amazon Titan Text Embeddings V2 embedding (`normalize=true`)
of a normalized one-paragraph description of a real failure. The index is:

```sql
CREATE VECTOR INDEX fs_sem ON failure_signatures (stage, error_class, embedding vector_cosine_ops);
```

The prefix columns `(stage, error_class)` are not decorative. CockroachDB only
accelerates a *filtered* vector search when the filters match prefix columns,
and every search Cairn issues is filtered — the agent loop always scopes to a
stage, and usually to an error class. Indexing on `(embedding)` alone would
have produced a correct index that the real query pattern could not use.

**Visible behaviour.** Cairn blocks a configuration it has never seen, because
it is semantically the same mistake as one it has. That is the
`strong_semantic` tier: cosine distance ≤ 0.15, same stage and error class, and
at least one *causal* structured feature matching exactly.

**Why in CockroachDB rather than a dedicated vector database.** The vector
search result is joined, **in the same transaction**, against `remediations`,
`runs`, and `work_claims` to produce an actionable decision. Vectors,
relational rows, and claim state have to be transactionally consistent: a
remediation that is committed but whose embedding is not yet visible produces a
*wrong decision*, not merely a stale read. One store, one transaction, no
dual-write.

**Degradation, and it is honest.** Vector index availability varies by
CockroachDB plan and build. `db/memory.py::ensure_vector_index` attempts
creation and **reports which path is active instead of raising**;
`vector_index_status` reads that state without side effects, and `cairn doctor`
surfaces it. With no index, `search` runs an exact brute-force cosine scan via
`<=>` — correct, slower, and entirely adequate at demo cardinality. A missing
index is a performance fact, never a correctness gap.

**Without it.** Only `exact` matches survive — memory degrades from "recognizes
the same mistake phrased differently" to "recognizes a byte-identical config".
The single most distinctive behaviour in the system disappears.

---

## 2. Cloud Managed MCP Server

**What it does here.** The **Memory Inspector** panel is a Bedrock Claude agent
whose tools are the MCP server's `list_tables`, `get_table_schema`,
`select_query`, and `explain_query`, pointed at
`https://cockroachlabs.cloud/mcp`. A judge types a question in English and gets
a real answer from a real query against the live cluster, **with the executed
SQL displayed underneath**.

**Why load-bearing.** A memory is only useful if it is interrogable. This is
the demonstrable difference between "we have a database" and "you can ask the
memory questions" — and showing the SQL is what makes the answer checkable
rather than merely fluent.

**Constraints, enforced rather than described.** PROJECT.md §6.2 commits to a
20 s query timeout, a 10 KiB response cap, a 25-row default limit, and no
`crdb_internal` access. All four live in one place
(`src/cairn/console/sqltools.py`) and apply to every backend. Enforcement is
three independent layers, none trusted alone:

1. A keyword and shape guard that runs *before* the database is contacted —
   single statement only, must begin `SELECT`/`WITH`, word-boundary refusal of
   DDL, DML, session control, and `crdb_internal`.
2. `SET TRANSACTION READ ONLY`, so CockroachDB itself rejects a write.
3. A `SELECT`-only SQL role (`db/migrations/0008_console_readonly_role.sql`),
   so the credential cannot write even if the code tried.

**Status, stated plainly.** The MCP transport (JSON-RPC over MCP's
Streamable-HTTP) is implemented to spec but is **unverified against the live
server** — it needs a CockroachDB Cloud service-account key, which the
development environment does not have. So `sqltools.py` implements the same
four tool contracts twice: `McpToolBackend` for the real server, and
`DirectSqlToolBackend` executing the identical contracts over pgwire.
**Which backend served a request is always reported** in the API response and
rendered in the panel. An answer produced over pgwire never claims to have come
from the MCP server.

One architectural note: this agent runs on Amazon Bedrock, which does not offer
a server-side MCP connector. The MCP session is therefore driven client-side by
the console process and its tools are handed to Claude as ordinary tool
definitions. Same four tools, same server — the connection is just made from
the console rather than from Anthropic's infrastructure.

**Without it.** The console still shows what the memory contains. It stops being
something you can *ask*.

---

## 3. `ccloud` CLI

Used in three real places, none of them ceremonial:

- **Provisioning.** `scripts/provision_cluster.sh` runs `ccloud cluster
  create`, `ccloud cluster user create` for the app role, and `ccloud cluster
  sql --url` to obtain the connection string, which it writes to `.env`.
  Teardown is `ccloud cluster delete` via `make teardown`. The cluster is
  scripted, reproducible, and disposable — which is also the cost control
  (see `docs/COST.md`).
- **CI.** A service-account API key drives `ccloud` non-interactively to create
  an **ephemeral verification cluster per pull request**, apply migrations, run
  the integration suite including the race test against real CockroachDB, and
  delete the cluster. No mocks in the concurrency tests — that rule is absolute
  in this repo. (The workflow job exists and is gated on the secret being
  present; see `.github/workflows/ci.yml`.)
- **Runtime.** `cairn doctor` shells `ccloud cluster list --json` to report
  cluster identity, regions, and plan alongside Cairn's own health checks, so
  an operator sees the memory layer's topology from inside the product rather
  than from a separate console tab.

**Without it.** Setup becomes a manual, unreproducible sequence of web-console
clicks, and the "no mocked database" guarantee in CI becomes unaffordable.

---

## 4. Agent Skills Repo

`cockroachlabs/cockroachdb-skills` (Apache-2.0), installed at `.agents/skills/`
via `npx skills add cockroachlabs/cockroachdb-skills` and committed to the repo.

These are not decoration: they changed the schema and the transaction code, and
`docs/SKILLS_USAGE.md` records which skill informed which file, with the
before/after numbers. The four concrete changes:

1. **Retry scope.** The original `with_retry` wrapper retried only the failing
   statement. The transaction/resilience skill made clear that under
   `SERIALIZABLE` the *entire transaction* must be replayed on SQLSTATE
   `40001`. `db/txn.py` was rewritten to take a closure and replay it whole.
   This is the single change that makes the claim protocol correct under
   contention.
2. **`SELECT ... FOR UPDATE` on the contended read.** The claim-contention path
   originally did a plain `SELECT` after a failed insert. Locking the row early
   cut observed `40001` retries in the two-region race test from a mean of 3.1
   to 0.4 per contended claim.
3. **Vector index prefix columns.** The performance skill plus the vector-index
   documentation established that filtered vector search is only accelerated
   when the filters match prefix columns — which is why the index is
   `(stage, error_class, embedding vector_cosine_ops)` and not `(embedding)`.
4. **Batch-insert avoidance.** Vector rows are inserted one at a time, per
   documented guidance that large batch inserts of `VECTOR` degrade
   performance. `record_failure_signature` is deliberately not batchable.

**Without it.** Item 1 is a correctness bug under contention, and items 2–4 are
measurable performance regressions. The skills are the reason those are not
present.

---

# AWS — 6 services

| Service | Role in Cairn | Load-bearing because |
|---|---|---|
| **Amazon Bedrock** | `anthropic.claude-sonnet-5` for change classification, probe selection, remediation authoring, and the Memory Inspector agent. `amazon.titan-embed-text-v2:0` (1024-d, `normalize=true`) for failure embeddings. | Classifying an ambiguous diff and authoring a remediation are the two genuinely non-deterministic judgments in the system. Embeddings are what make `strong_semantic` matching exist at all. |
| **ECS Fargate** | Runs the real workload as tasks in **two regions** (us-east-1, us-west-2), and hosts the console. | Two regions against one cluster is what makes the claim race a real distributed race rather than two processes on one box. |
| **Amazon S3** | Content-addressed artifact and fragment storage; vendored dataset and model snapshots. | Reuse only means anything if the artifact is actually fetchable by a *different* machine. Fragment durability is what makes crash resume real rather than theoretical. |
| **AWS Lambda** | The lease reaper — finds expired leases and marks them takeover-eligible. Also the `cairn-webhook` endpoint for CI plan comments. | The reaper must run **when no worker is alive**. It cannot live in the worker; that is the whole point. |
| **Amazon EventBridge** | 30-second schedule driving the reaper. | Bounded takeover latency. Without a schedule, an expired lease is discovered only by luck. |
| **Amazon CloudWatch** | Structured JSON logs and custom metrics (`ReuseRate`, `DuplicatesPrevented`, `FailuresAvoided`, `ProbeLatencyP50`, `TxnRetries40001`), plus 4 alarms. | Product readiness is a judged criterion, and a system that makes autonomous reuse decisions has to be observable to be trusted. |

CloudFront fronts the ALB for the public demo URL and TLS.

## Two AWS details worth stating explicitly

**The correctness path never depends on the LLM.** `--no-llm` (or
`CAIRN_NO_LLM`) degrades ambiguous-change classification and remediation
authoring to the deterministic rule path. `exact` matches, every structural
class, and all six probes still work with Bedrock entirely unavailable. What
degrades is `strong_semantic`/`weak` matching (no embeddings) and the Memory
Inspector (which reports itself unavailable rather than answering from the
schema alone). Every one of those degradations is surfaced in the UI with the
real underlying cause, not swallowed.

**Bedrock needs two distinct IAM actions, not one.** The Titan embedding path
calls `bedrock-runtime` and authorizes against `bedrock:InvokeModel` on a
foundation-model ARN. The Claude path uses `AnthropicBedrockMantle` — required
because Sonnet 5's `thinking`/`output_config` parameters are only accepted
through that client — and authorizes against `bedrock-mantle:CreateInference`
on a *project* ARN. A principal holding only `InvokeModel` gets a 403 naming
exactly that missing action. `infra/iam.tf` grants both, scoped to the two
model ARNs and the one project ARN; granting only the first would leave the
Memory Inspector 403-ing while every other panel worked.

---

# Requirement coverage

| Requirement | Where |
|---|---|
| ≥ 2 CockroachDB tools, used meaningfully | All four above, each tied to a visible behaviour and a stated "without it" |
| ≥ 1 AWS service, used meaningfully | Six above |
| CockroachDB as load-bearing persistent memory | Claims, causal graph, negative memory, decisions, and fragments are all CockroachDB tables; every console panel reads them live |
| Deployed on AWS | ECS Fargate ×2 regions, S3, Bedrock, Lambda, EventBridge, CloudWatch, CloudFront |
| Explain what each tool does | This document, plus `PROJECT.md` §6.2/§6.3 |

See also: [`ARCHITECTURE.md`](ARCHITECTURE.md) for how these fit together,
[`PROBES.md`](PROBES.md) for what the evidence layer does and does not prove,
[`SKILLS_USAGE.md`](SKILLS_USAGE.md) for the skills' file-level impact, and
[`COST.md`](COST.md) for the spend guardrails.
