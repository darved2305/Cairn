# Probes: what each one proves, and what it does not

Cairn's central claim is that reuse can be *authorized by deterministic
evidence* rather than assumed from a hash match. A probe is that evidence. This
document states, for each of the six probe types, exactly what a passing result
establishes — and, with equal prominence, what it does not.

The non-guarantees are not a disclaimer section. They are the reason the system
is trustworthy. A tool that recomputes a bounded sample and then claims full
equivalence has replaced one unjustified assumption with another. Cairn's rule
is the opposite:

> **Cairn never claims a probe proves full artifact equivalence.**

Every probe run is recorded in `probe_runs` with its `probe_type`,
`sample_spec` (the exact, deterministic selection rule), `population_size`,
`sample_size`, `tolerance`, `runtime_ms`, `passed`, and `evidence_digest`. The
console renders sample and population **as a fraction, always** — never as a
percentage and never as a bare "verified" badge — because 64/2400 and "passed"
are very different statements and only one of them is true.

---

## The authority model these probes sit inside

A probe is one of three things that can authorize reuse. `reuse_decisions`
records which one did, in `authorized_by`:

| Authority | Meaning |
|---|---|
| `identity` | The work key itself is unchanged. Nothing was recomputed because nothing could have changed. |
| `structural` | A reachability proof established that the changed code cannot execute in this stage. |
| `probe` | A bounded recomputation was compared against the recorded artifact and matched. |

There is no fourth value. In particular there is no `model`: an LLM may propose
reuse and may select which probe would discriminate, but a database `CHECK`
constraint makes a model-authorized reuse *unrepresentable*, not merely
discouraged. If a proposal reaches `commit_decision` without a passing probe or
a passing structural proof, it is written as `verdict='refused'` and the stage
recomputes.

**Conservative mode is the default and is what the demo runs.** In conservative
mode `atol = 0` — comparisons are bitwise. If no probe is defined for an
artifact class, the stage recomputes. If the environment fingerprint differs at
all, the stage recomputes, because no probe is trusted across environments.

---

## P1 — `env_identity`

**What it does.** Compares the `env_fingerprint` of the candidate artifact
against the current environment. The fingerprint hashes the base image digest,
the Python version, the full sorted resolved dependency set, and the configured
torch thread count.

**Proves.** The base image digest, Python version, and complete resolved
dependency set are identical. Every other probe's result is only meaningful
under this one, which is why it gates them.

**Does not prove.**
- Identical CPU microarchitecture.
- Identical BLAS kernel selection.
- Absence of nondeterminism from thread scheduling.

Cairn pins `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, `PYTHONHASHSEED=0`,
`torch.manual_seed(1337)`, and `torch.use_deterministic_algorithms(True)` in the
container, which closes that gap **for the workload Cairn runs**. It is not
closed in general. A different workload on the same image could still observe
nondeterminism that P1 cannot see, and P1 makes no claim about it.

---

## P2 — `structural_unreachable`

**What it does.** Computes reachability from the stage entrypoint over the
*union* of the old and new call/import graphs, extracted from Python ASTs.

**Proves.** Under a stated restriction, the changed code cannot be executed by
this stage.

**Does not prove.** Anything at all, if the restriction is violated. Python's
dynamic import and dispatch make static reachability an approximation, not a
proof, so Cairn **hard-refuses** the class whenever any of the following appear
anywhere in the reachable set:

`importlib` · `__import__` · `getattr`/`setattr` dispatch · `globals()` ·
`vars()` · `eval` · `exec` · plugin-registry decorators · entry-point loading

That refusal is the load-bearing part. A reachability result is treated as
valid *only* under that restriction; a violation is a hard refusal rather than
a warning, because a warning would let an unsound proof authorize reuse.

---

## P3 — `feature_resample`

**What it does.** Recomputes `k = 64` rows, selected deterministically by
`sha256(artifact_id ‖ row_id) mod N`, and compares the `sha256` of the
canonical little-endian float32 byte encoding of each.

**Proves.** Those 64 rows are bit-identical.

**Does not prove.** That the other rows are. With a 2,400-row feature table
this is a **64/2400 sample**, and that fraction is what the UI displays. This is
a sampling argument, and Cairn presents it as one.

**What makes it usable anyway.** P3 is never the only evidence. It runs on top
of a passing P1 and a passing structural class. For the change classes Cairn
admits — `comment_only`, `formatting_only`, `logging_only`,
`unreachable_change` — the residual risk is that a change altered the
computation for some rows but not for a hash-selected 64. That requires the
structural proof to have been wrong, which in turn requires a dynamic-dispatch
escape that P2 already hard-refuses on. The layers are the argument; no single
layer is.

---

## P4 — `checkpoint_logit`

**What it does.** Loads the checkpoint, runs a fixed 128-example batch
(deterministic selection, `model.eval()`, `torch.inference_mode()`), and
compares the resulting logits.

**Tolerance.** Conservative mode compares bitwise, `atol = 0`. That is only
defensible because P1 passed and determinism is pinned; a bitwise comparison
across differing environments would be meaningless, which is why env drift is a
hard refusal rather than a tolerance question.

**Proves.** The checkpoint produces identical outputs on those 128 inputs.

**Does not prove.** Identical outputs on all inputs. It cannot detect a change
that alters behaviour only outside the probe batch.

**One structural limitation worth stating plainly:** P4 requires a *second*
checkpoint to already exist to compare against, so it cannot itself be what
avoids paying for that checkpoint. In the agent loop, `checkpoint` and `eval`
recompute for real on a work-key change; probe-authorized reuse-without-recompute
is applied to the `features` stage, where P1 and P3 are both genuinely cheap.
That is the designed behaviour, not a shortfall of it.

---

## P5 — `schema_stats`

**What it does.** Compares the schema (column names, dtypes, and order), the
row count, and a per-column checksum computed over a deterministic 10%
partition slice.

**Proves.** Structural conformance, and content equality within the slice.

**Does not prove.** Content equality outside the slice. A change that altered
only rows in the other 90% passes this probe.

---

## P6 — `eval_slice_replay`

**What it does.** Recomputes the evaluation metric on a fixed 200-example slice
and compares bitwise.

**Proves.** The evaluation code produces the same number on the same slice.

**Does not prove.** That the full-set metric is unchanged. A metric is an
aggregate; agreement on a slice is not agreement on the aggregate.

---

## Summary table

| Probe | Proves | Does **not** prove |
|---|---|---|
| **P1** `env_identity` | Image digest, Python version, and resolved dependency set are identical | Identical microarchitecture, BLAS kernel selection, or freedom from thread-scheduling nondeterminism |
| **P2** `structural_unreachable` | Under the stated restriction, the changed code cannot execute in this stage | Anything, if `importlib`/`__import__`/`getattr`-dispatch/`eval`/`exec`/entry-points appear in the reachable set — hence the hard refusal |
| **P3** `feature_resample` | 64 hash-selected rows are bit-identical | That the remaining rows are. It is a sample, displayed as a fraction |
| **P4** `checkpoint_logit` | The checkpoint gives identical logits on 128 fixed examples | Identical outputs on all inputs; blind to changes outside the batch |
| **P5** `schema_stats` | Schema conformance and slice-level content equality | Content equality outside the 10% slice |
| **P6** `eval_slice_replay` | The metric is unchanged on a fixed 200-example slice | That the full-set metric is unchanged |

---

## Where to see this in the running system

- **Console → Causal Graph** — click any node with a decision. The evidence
  drawer shows the class that applied, the probe type, `sample_size /
  population_size` as a fraction, the tolerance, the runtime, and the exact
  `sample_spec` selection rule.
- **`cairn explain <artifact_id>`** — the same evidence chain from the CLI.
- **`probe_runs`** — the table itself. Every column named above is a real
  column; nothing in the UI is computed from anything else.

## The failure mode this does not cover, and what catches it

A probe can pass and the artifact can still be stale — that is what "does not
prove" means. Cairn's answer is not a better probe; it is to assume the
possibility and detect it after the fact. When a later run produces evidence
contradicting an earlier reuse, Cairn writes a `contradictions` row, sets
`artifacts.quarantined_at`, invalidates every `reuse_decision` that cited the
artifact, and raises an alarm. Quarantine is one-way: reversing it requires an
explicit, audited `cairn unquarantine <artifact_id> --reason "<text>"`.

That mechanism is what makes "the model may propose reuse" survivable. If the
deterministic authority was ever wrong, the system finds out and stops trusting
the artifact.
