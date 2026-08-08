"""The agent loop — PROJECT.md §6.4.

    perceive   -> planner.plan_pipeline (work keys, reachability, config reads)
    recall     -> db/claims.acquire (causal graph) + db/memory.search (negative memory)
    decide     -> one of the nine actions (agent/actions.py)
    act        -> claim / subscribe / probe / launch / resume / refuse
    learn      -> write decision, probe, fragments, artifact, failure signature, remediation

Scope, stated honestly rather than left implicit: reuse-without-recompute
is authorized two ways here — `identity` (the work_key itself is unchanged,
handled for free by `claims.acquire` returning a SUCCEEDED hit) and `probe`
(P1 env-identity + P3 feature-resample), applied only to the `features`
stage, where both are genuinely cheap. `checkpoint` and `eval` always
recompute for real on a work_key change — P4 checkpoint_logit requires a
*second* checkpoint to already exist to compare against, so it cannot
itself be what avoids paying for that checkpoint; PLAN.md's own D5 exit
bar ("hidden_dim change -> features reused, checkpoint+eval recomputed")
is this exact behaviour, not a shortfall of it. `classify/rules.py`'s six
change classes remain available, correct, and independently tested for a
git-diff-based caller (e.g. a future CI `cairn plan` enhancement); this
loop does not yet have access to historical old/new source per unit, so it
does not invoke them.

Pre-flight `REFUSE_DOOMED` blocking is likewise scoped to the one case this
codebase can derive an `error_class_hint` for *before* running anything:
`checkpoint`'s embedding_dim/input_dim shape mismatch, mirroring the exact
failure `stage_train.run_epochs` raises and the exact scenario
PLAN.md D6's exit bar names. Other stages skip the pre-flight memory
check entirely rather than guess an error class with no real basis for one
-- `db/memory.py::tier` requires `match.error_class == candidate.error_class_hint`
for both blocking tiers, so a hint-less search can structurally never block,
and paying for an embedding call that can never affect the outcome is not
worth doing.
"""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any

import numpy as np
import numpy.typing as npt
import psycopg
import torch
from psycopg_pool import ConnectionPool

from cairn.agent.actions import Action
from cairn.classify import llm as classify_llm
from cairn.config import TrackedConfig
from cairn.db import claims, memory
from cairn.db.contradictions import record_contradiction
from cairn.db.decisions import ReuseDecision, record_decision, record_decision_with_probe
from cairn.db.environments import ensure_environment
from cairn.db.fragments import clear_fragments, list_fragments, record_fragment
from cairn.db.graph import (
    ArtifactRow,
    get_artifact,
    latest_succeeded_artifact,
    load_artifact_inputs,
)
from cairn.db.txn import in_txn
from cairn.embeddings import default_provider
from cairn.obs.events import emit_event
from cairn.planner import STAGES, PipelinePlan, StagePlan, artifact_inputs, plan_pipeline
from cairn.probes import p1_env, p3_features
from cairn.probes.base import ProbeResult
from cairn.storage import s3
from cairn.workload import stage_dataset, stage_env, stage_eval, stage_features, stage_train
from cairn.workload.stage_env import EnvManifest

# PROJECT.md §5.2's approximate per-stage timings, used only as a cost-
# projection fallback when a stage has no prior artifacts yet to average a
# real duration from. AWS Fargate on-demand, Linux/x86, us-east-1 —
# published rates, stable for years, matching cost_rates.source_note's own
# documented format.
_DEFAULT_DURATION_S = {"env": 2, "dataset": 9, "features": 95, "checkpoint": 28, "eval": 4}
_FARGATE_VCPU_HOUR_USD = 0.04048
_FARGATE_GB_HOUR_USD = 0.004445
_VCPU = 2.0
_MEM_MIB = 4096
_INSTANCE_KIND = "fargate-2vcpu-4gib"


class Escalation(RuntimeError):
    """Raised for ESCALATE — the CLI catches this, prints the reason, and
    exits non-zero without having spent the compute or asked a probe to
    run. Approval is the only action that stops the loop for a human."""


class Refusal(RuntimeError):
    """Raised for REFUSE_DOOMED / REFUSE_DUPLICATE — a real, terminal,
    non-error outcome that still has to stop `cairn run`/`cairn plan`
    (with a non-zero exit) before any compute happens."""


class UpstreamArtifactUnavailable(RuntimeError):
    """An S3 read failed for a specific artifact adopted from memory.

    This explicit attribution is what makes quarantine causal. A generic
    downstream exception (for example, a missing independently-vendored raw
    dataset) is not evidence against whichever upstream happened to be
    reused earlier in the run.
    """

    def __init__(self, artifact: ArtifactHandle, detail: str) -> None:
        self.artifact_id = artifact.artifact_id
        super().__init__(
            f"upstream artifact {artifact.artifact_id} ({artifact.s3_uri}) unavailable: {detail}"
        )


class _ClaimHeartbeat:
    """Keep a real workload's lease alive while compute blocks the main thread."""

    def __init__(
        self,
        pool: ConnectionPool,
        work_key: str,
        owner: str,
        fence: int,
        *,
        interval_s: float = claims.HEARTBEAT_SECONDS,
    ) -> None:
        self._pool = pool
        self._work_key = work_key
        self._owner = owner
        self._fence = fence
        self._interval_s = interval_s
        self._stop = Event()
        self._lost = Event()
        self._error: Exception | None = None
        self._thread = Thread(target=self._run, name=f"cairn-heartbeat-{work_key[:8]}", daemon=True)

    def __enter__(self) -> _ClaimHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval_s + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                if not claims.heartbeat(self._pool, self._work_key, self._owner, self._fence):
                    self._lost.set()
                    return
            except Exception as exc:  # the main thread must fail closed before commit
                self._error = exc
                self._lost.set()
                return

    def require_owned(self) -> None:
        if self._lost.is_set():
            detail = f": {self._error}" if self._error is not None else ""
            raise RuntimeError(
                f"lost claim heartbeat for work_key={self._work_key} (fence={self._fence}){detail}"
            )


@dataclass(frozen=True)
class ArtifactHandle:
    artifact_id: str
    s3_uri: str
    stage: str
    work_key: str
    env_fingerprint: str

    @classmethod
    def from_row(cls, row: ArtifactRow) -> ArtifactHandle:
        return cls(
            artifact_id=row.artifact_id,
            s3_uri=row.s3_uri,
            stage=row.stage,
            work_key=row.work_key,
            env_fingerprint=row.env_fingerprint,
        )


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    work_key: str
    action: Action
    verdict: str
    artifact: ArtifactHandle | None
    decision_id: uuid.UUID | None
    detail: str


def _get_upstream_bytes(bucket: str, artifact: ArtifactHandle) -> bytes:
    key = artifact.s3_uri.split("/", 3)[-1]
    try:
        return s3.get_bytes(bucket, key)
    except Exception as exc:
        raise UpstreamArtifactUnavailable(artifact, str(exc)) from exc


# ---------------------------------------------------------------------------
# cost projection — ESCALATE gate
# ---------------------------------------------------------------------------


def _ensure_cost_rates(pool: ConnectionPool) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO cost_rates (resource_kind, usd, source_note) VALUES
              ('fargate_vcpu_hour', %s, 'AWS published on-demand us-east-1, 2026-08'),
              ('fargate_gb_hour', %s, 'AWS published on-demand us-east-1, 2026-08')
            ON CONFLICT (resource_kind) DO NOTHING
            """,
            (_FARGATE_VCPU_HOUR_USD, _FARGATE_GB_HOUR_USD),
        )

    in_txn(pool, _tx, op="agent.ensure_cost_rates")


def estimated_cost_usd(pool: ConnectionPool, stage: str) -> tuple[float, str]:
    """Projected dollar cost for one run of `stage`, with the formula
    spelled out in the returned detail string — Definition of Done: "every
    number in the UI is measured or shown with its formula." Duration
    comes from a real average of this stage's own prior artifacts when any
    exist, else the PROJECT.md §5.2 fallback above."""

    _ensure_cost_rates(pool)

    def _tx(cur: psycopg.Cursor) -> tuple[dict[str, float], float | None]:
        cur.execute(
            "SELECT resource_kind, usd FROM cost_rates "
            "WHERE resource_kind IN ('fargate_vcpu_hour','fargate_gb_hour')"
        )
        rates = {kind: float(usd) for kind, usd in cur.fetchall()}
        cur.execute("SELECT avg(duration_ms) FROM artifacts WHERE stage=%s", (stage,))
        row = cur.fetchone()
        avg_ms = float(row[0]) if row and row[0] is not None else None
        return rates, avg_ms

    rates, avg_ms = in_txn(pool, _tx, op="agent.estimated_cost")
    if avg_ms is not None:
        duration_s = avg_ms / 1000.0
        source = f"avg of prior {stage} artifacts"
    else:
        duration_s = float(_DEFAULT_DURATION_S.get(stage, 30))
        source = "PROJECT.md §5.2 default (no prior artifacts to average)"
    hours = duration_s / 3600.0
    cost = (
        rates["fargate_vcpu_hour"] * _VCPU * hours
        + rates["fargate_gb_hour"] * (_MEM_MIB / 1024.0) * hours
    )
    detail = (
        f"${cost:.6f} = ({rates['fargate_vcpu_hour']:.5f}*{_VCPU}vcpu + "
        f"{rates['fargate_gb_hour']:.6f}*{_MEM_MIB / 1024.0:.1f}GiB) * "
        f"{duration_s:.1f}s/3600 ({source})"
    )
    return cost, detail


# ---------------------------------------------------------------------------
# negative-memory recall — scoped to the one pre-flight case this codebase
# can derive an error_class_hint for without running anything (see module
# docstring).
# ---------------------------------------------------------------------------


def _checkpoint_shape_mismatch(
    config_reads: Mapping[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Mirrors stage_train.run_epochs's own guard
    (`x_train.shape[1] != input_dim`) without running anything — returns
    (traceback_head, structured) when the configured input_dim provably
    cannot match the features stage's own configured embedding_dim, else
    None. `structured` matches db/memory.py::PlanFeatures's shape.
    `config_reads` is `run_stage`'s checkpoint stage_plan.config_reads plus
    the synthetic "_features_embedding_dim" key it adds from the *plan's*
    features stage — not from an upstream artifact, since this check has
    to work before anything has been claimed or computed."""

    input_dim = config_reads.get("train.input_dim")
    embedding_dim = config_reads.get("_features_embedding_dim")
    if input_dim is None or embedding_dim is None or input_dim == embedding_dim:
        return None
    traceback_head = f"ValueError: training input dimension is {embedding_dim}, configured input_dim is {input_dim}"
    structured = {
        "framework": "pytorch",
        "framework_version": torch.__version__,
        "embedding_dim": embedding_dim,
        "instance_kind": _INSTANCE_KIND,
        "vcpu": _VCPU,
        "mem_mib": _MEM_MIB,
    }
    return traceback_head, structured


def _recall_blocking_match(
    pool: ConnectionPool,
    *,
    stage: str,
    error_class_hint: str,
    structured: dict[str, object],
    summary_text: str,
) -> memory.TieredMatch | None:
    """`summary_text` must actually describe the *specific* proposed
    values (e.g. the real embedding_dim/input_dim numbers) — a generic
    "checkpoint stage proposed: ValueError" string would embed identically
    for every mismatch this loop ever checks, making cosine similarity
    meaningless. Callers pass the real traceback_head-shaped text for
    exactly this reason (see `_checkpoint_shape_mismatch`)."""

    candidate = memory.PlanFeatures(
        stage=stage, error_class_hint=error_class_hint, structured=structured
    )
    embedding = default_provider().embed(summary_text)
    matches = memory.search(
        pool, stage=stage, error_class_hint=error_class_hint, embedding=embedding
    )
    for match in matches:
        tiered = memory.tier(match, candidate)
        if tiered.tier in memory.BLOCKING_TIERS:
            return tiered
    return None


def _apply_remediation(
    config: TrackedConfig, changed_keys: list[tuple[str, object]]
) -> TrackedConfig:
    """Mutate a copy of `config`'s underlying data at each `(key, value)`.
    `key` is a fully dotted path (e.g. "train.input_dim") when it contains
    a '.'; the bare key "embedding_dim" — the shape remediations recorded
    against the one pre-flight check this loop performs actually use, see
    tests/integration/test_d6_exit.py — is special-cased to
    "train.input_dim", the config value that check actually reads."""

    mutated: dict[str, Any] = config.to_dict()
    for key, value in changed_keys:
        dotted = key if "." in key else ("train.input_dim" if key == "embedding_dim" else key)
        parts = dotted.split(".")
        cursor = mutated
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return TrackedConfig(mutated)


# ---------------------------------------------------------------------------
# stage compute — real work, with fragment persistence for the two
# fragmentable stages (PROJECT.md §4.5).
# ---------------------------------------------------------------------------


def _persist_features_fragments(
    pool: ConnectionPool,
    df: Any,
    *,
    work_key: str,
    run_id: uuid.UUID,
    fence: int,
    bucket: str,
    shard_count: int,
    batch_size: int,
    max_seq_length: int,
    model_name: str,
    embedding_dim: int,
) -> tuple[list[int], npt.NDArray[np.float32]]:
    """Resumable: skips any shard index already recorded in `run_fragments`
    for this work_key, verifying it against S3 before trusting it. Each
    shard is independently computed (no cross-shard state), so resuming a
    subset of shards is unconditionally safe."""

    existing = {f.fragment_index: f for f in list_fragments(pool, work_key)}
    bounds = stage_features._shard_bounds(len(df), shard_count)  # noqa: SLF001
    all_doc_ids: list[int] = []
    shard_arrays: list[npt.NDArray[np.float32]] = []

    for shard_index, (start, end) in enumerate(bounds):
        if shard_index in existing:
            frag = existing[shard_index]
            data = s3.get_fragment_verified(bucket, work_key, shard_index, frag.content_digest)
            doc_ids, embeddings = stage_features.read_parquet(data)
            all_doc_ids.extend(doc_ids)
            shard_arrays.append(embeddings)
            continue

        chunk = df.iloc[start:end]
        model = stage_features._get_model(max_seq_length, model_name)  # noqa: SLF001
        with_timer_start = time.monotonic()
        embeddings = np.asarray(
            model.encode(
                chunk["text"].tolist(),
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=False,
            ),
            dtype=np.float32,
        )
        if embeddings.shape[1] != embedding_dim:
            raise ValueError(
                f"embedding model returned dimension {embeddings.shape[1]}, "
                f"config declares {embedding_dim}"
            )
        doc_ids = chunk["doc_id"].tolist()
        parquet_bytes = stage_features._write_parquet(doc_ids, embeddings)  # noqa: SLF001
        s3_uri, digest = s3.put_fragment(bucket, work_key, shard_index, parquet_bytes)
        record_fragment(
            pool,
            work_key,
            shard_index,
            run_id=run_id,
            fence=fence,
            s3_uri=s3_uri,
            content_digest=digest,
            duration_ms=int((time.monotonic() - with_timer_start) * 1000),
        )
        all_doc_ids.extend(doc_ids)
        shard_arrays.append(embeddings)

    return all_doc_ids, np.concatenate(shard_arrays, axis=0)


def _run_checkpoint_with_fragments(
    pool: ConnectionPool,
    joined: Any,
    *,
    work_key: str,
    run_id: uuid.UUID,
    fence: int,
    bucket: str,
    num_labels: int,
    epochs: int,
    input_dim: int,
    hidden_dim: int,
    learning_rate: float,
    batch_size: int,
) -> Any:
    """Persists each epoch's fragment for forensics/progress visibility.
    True resume is only attempted for the trivial-but-real case — the
    final epoch's fragment already exists and verifies (crashed after the
    last epoch but before `claims.complete`) — because `run_epochs`
    restarts training from a fresh model/optimizer/RNG generator on every
    call; splicing a *partial* training run back in without also restoring
    optimizer state and the exact RNG stream position would be a real
    determinism risk this module is not going to take on silently. An
    earlier-epoch crash re-trains from epoch 0, correctly, just not
    maximally cheaply."""

    existing = {f.fragment_index: f for f in list_fragments(pool, work_key)}
    final_index = epochs - 1
    if final_index in existing:
        frag = existing[final_index]
        state_dict_bytes = s3.get_fragment_verified(
            bucket, work_key, final_index, frag.content_digest
        )
        return stage_train.CheckpointArtifact(
            state_dict_bytes=state_dict_bytes, final_train_loss=float("nan"), epochs=epochs
        )

    train_mask = np.array([s == "train" for s in joined.split], dtype=bool)
    x_train = joined.embeddings[train_mask]
    y_train = joined.labels[train_mask]

    last = None
    for fragment in stage_train.run_epochs(
        x_train,
        y_train,
        num_labels=num_labels,
        epochs=epochs,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        learning_rate=learning_rate,
        batch_size=batch_size,
    ):
        s3_uri, digest = s3.put_fragment(
            bucket, work_key, fragment.epoch_index, fragment.state_dict_bytes
        )
        record_fragment(
            pool,
            work_key,
            fragment.epoch_index,
            run_id=run_id,
            fence=fence,
            s3_uri=s3_uri,
            content_digest=digest,
            duration_ms=0,
        )
        last = fragment
    assert last is not None, "epochs must be >= 1"
    return stage_train.CheckpointArtifact(
        state_dict_bytes=last.state_dict_bytes, final_train_loss=last.train_loss, epochs=epochs
    )


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def run_stage(
    pool: ConnectionPool,
    *,
    plan: PipelinePlan,
    stage_name: str,
    config: TrackedConfig,
    bucket: str,
    owner: str,
    host: str,
    region: str,
    run_id: uuid.UUID,
    environment: EnvManifest,
    upstream: Mapping[str, StageOutcome],
    approval_usd: float = 0.50,
) -> StageOutcome:
    stage_plan = plan.by_stage()[stage_name]
    work_key = stage_plan.work_key.value

    # --- recall: negative memory (checkpoint's shape-mismatch pre-check only) ---
    if stage_name == "checkpoint":
        config_reads = dict(stage_plan.config_reads)
        features_plan = plan.by_stage().get("features")
        if features_plan is not None:
            config_reads["_features_embedding_dim"] = features_plan.config_reads.get(
                "features.embedding_dim"
            )
        mismatch = _checkpoint_shape_mismatch(config_reads)
        if mismatch is not None:
            traceback_head, structured = mismatch
            blocking = _recall_blocking_match(
                pool,
                stage=stage_name,
                error_class_hint="ValueError",
                structured=structured,
                summary_text=(
                    f"checkpoint stage failed with {traceback_head} — the embedding "
                    f"model produces {structured['embedding_dim']}-d vectors but "
                    f"train.input_dim was still {config_reads.get('train.input_dim')}"
                ),
            )
            if blocking is not None:
                if blocking.match.causal_keys:
                    remediation = memory.latest_successful_remediation(
                        pool, blocking.match.signature_id
                    )
                else:
                    remediation = None
                if remediation is not None:
                    fixed_config = _apply_remediation(
                        config, [(ck.key, ck.to_value) for ck in remediation.changed_keys]
                    )
                    fixed_plan = plan_pipeline(
                        fixed_config,
                        source_root="src",
                        env_fingerprint=plan.env_fingerprint,
                        data_fingerprint=plan.data_fingerprint,
                    )
                    return _replan_and_recurse(
                        pool,
                        plan=fixed_plan,
                        stage_name=stage_name,
                        config=fixed_config,
                        bucket=bucket,
                        owner=owner,
                        host=host,
                        region=region,
                        run_id=run_id,
                        environment=environment,
                        upstream=upstream,
                        approval_usd=approval_usd,
                        remediation_note=(
                            f"REMEDIATE_AND_REPLAN: applied {remediation.rationale!r}, "
                            f"changed {[ck.key for ck in remediation.changed_keys]}"
                        ),
                    )
                decision_id = record_decision(
                    pool,
                    ReuseDecision(
                        work_key=work_key,
                        stage=stage_name,
                        action=Action.REFUSE_DOOMED.value,
                        verdict="refused",
                        proposed_by="rule",
                        latency_ms=0,
                        explanation=(
                            f"negative memory match at {blocking.tier.value}, no verified "
                            f"remediation on record: {traceback_head}"
                        ),
                    ),
                )
                raise Refusal(f"REFUSE_DOOMED work_key={work_key} decision_id={decision_id}")

    # --- recall: causal graph (claim protocol) ---
    claim = claims.acquire(pool, work_key, stage_name, owner, host, region, run_id)

    if not claim.won:
        if claim.reuse_artifact_id is not None:
            row = get_artifact(pool, claim.reuse_artifact_id)
            assert row is not None, "SUCCEEDED claim must reference a real artifact row"
            decision_id = record_decision(
                pool,
                ReuseDecision(
                    work_key=work_key,
                    stage=stage_name,
                    action=Action.REUSE.value,
                    verdict="reuse",
                    proposed_by="rule",
                    authorized_by="identity",
                    latency_ms=0,
                    explanation="work_key already SUCCEEDED — identity reuse, no compute",
                    candidate_artifact_id=row.artifact_id,
                ),
            )
            return StageOutcome(
                stage=stage_name,
                work_key=work_key,
                action=Action.REUSE,
                verdict="reuse",
                artifact=ArtifactHandle.from_row(row),
                decision_id=decision_id,
                detail="identity reuse",
            )

        result = claims.subscribe(pool, work_key)
        if result.terminal_state == "SUCCEEDED":
            assert result.artifact_id is not None
            row = get_artifact(pool, result.artifact_id)
            assert row is not None
            decision_id = record_decision(
                pool,
                ReuseDecision(
                    work_key=work_key,
                    stage=stage_name,
                    action=Action.SUBSCRIBE.value,
                    verdict="subscribed",
                    proposed_by="rule",
                    latency_ms=int(result.waited_s * 1000),
                    explanation=f"adopted artifact from owner={result.owner} after {result.waited_s:.1f}s",
                    candidate_artifact_id=row.artifact_id,
                ),
            )
            return StageOutcome(
                stage=stage_name,
                work_key=work_key,
                action=Action.SUBSCRIBE,
                verdict="subscribed",
                artifact=ArtifactHandle.from_row(row),
                decision_id=decision_id,
                detail=f"subscribed {result.waited_s:.1f}s",
            )
        # Owner terminated without succeeding (FAILED/ABANDONED) — the key
        # is open again now; one more acquire attempt claims it for real.
        claim = claims.acquire(pool, work_key, stage_name, owner, host, region, run_id)
        if not claim.won:
            decision_id = record_decision(
                pool,
                ReuseDecision(
                    work_key=work_key,
                    stage=stage_name,
                    action=Action.REFUSE_DUPLICATE.value,
                    verdict="refused",
                    proposed_by="rule",
                    latency_ms=0,
                    explanation="still contended after prior owner terminated",
                ),
            )
            raise Refusal(f"REFUSE_DUPLICATE work_key={work_key} decision_id={decision_id}")

    assert claim.fence is not None
    resumed = claim.took_over_from is not None and bool(list_fragments(pool, work_key))

    # --- decide: ESCALATE on projected cost ---
    cost, cost_detail = estimated_cost_usd(pool, stage_name)
    if cost > approval_usd:
        record_decision(
            pool,
            ReuseDecision(
                work_key=work_key,
                stage=stage_name,
                action=Action.ESCALATE.value,
                verdict="refused",
                proposed_by="rule",
                latency_ms=0,
                explanation=f"projected cost {cost_detail} exceeds CAIRN_APPROVAL_USD={approval_usd}",
            ),
        )
        claims.fail(pool, work_key, owner, claim.fence)
        emit_event(
            "approval.requested",
            {
                "work_key": work_key,
                "stage": stage_name,
                "projected_cost_usd": cost,
                "approval_usd": approval_usd,
                "detail": cost_detail,
            },
        )
        raise Escalation(f"ESCALATE work_key={work_key} {cost_detail}")

    # --- decide + act: probe-based reuse (features only) or real recompute ---
    started = time.monotonic()
    try:
        with _ClaimHeartbeat(pool, work_key, owner, claim.fence) as heartbeat:
            if stage_name == "features":
                outcome = _act_features(
                    pool,
                    stage_plan=stage_plan,
                    config=config,
                    bucket=bucket,
                    owner=owner,
                    region=region,
                    run_id=run_id,
                    fence=claim.fence,
                    environment=environment,
                    upstream=upstream,
                    resumed=resumed,
                )
            else:
                artifact_id, s3_uri, size_bytes = _act_generic(
                    pool,
                    stage_name=stage_name,
                    stage_plan=stage_plan,
                    config=config,
                    bucket=bucket,
                    run_id=run_id,
                    fence=claim.fence,
                    environment=environment,
                    upstream=upstream,
                )
                outcome = ("recompute", artifact_id, s3_uri, size_bytes, None)
            heartbeat.require_owned()
    except Exception as exc:
        claims.fail(pool, work_key, owner, claim.fence)
        _learn_from_failure(
            pool, stage=stage_name, run_id=run_id, exc=exc, stage_plan=stage_plan, upstream=upstream
        )
        raise

    kind, artifact_id, s3_uri, size_bytes, probe = outcome
    duration_ms = int((time.monotonic() - started) * 1000)

    record = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage=stage_name,
        work_key=work_key,
        s3_uri=s3_uri,
        size_bytes=size_bytes,
        env_fingerprint=environment.env_fingerprint,
        produced_by_run=run_id,
        duration_ms=duration_ms,
        vcpu=_VCPU,
        mem_mib=_MEM_MIB,
        region=region,
    )
    completed = claims.complete(
        pool, work_key, owner, claim.fence, record, inputs=artifact_inputs(stage_plan)
    )
    if not completed:
        raise RuntimeError(
            f"dispossessed while completing work_key={work_key} (fence={claim.fence})"
        )
    if stage_name in ("features", "checkpoint"):
        # A SUCCEEDED artifact makes the partial fragments moot — clear
        # them so a future attempt at this exact work_key (e.g. after
        # `cairn unquarantine`) never mistakes a stale fragment for
        # resumable progress on what would then be a fresh attempt.
        clear_fragments(pool, work_key)

    if kind == "reuse_probe":
        assert probe is not None
        decision_id, _ = record_decision_with_probe(
            pool,
            ReuseDecision(
                work_key=work_key,
                stage=stage_name,
                action=Action.REUSE.value,
                verdict="reuse",
                proposed_by="rule",
                authorized_by="probe",
                latency_ms=duration_ms,
                explanation=f"P1+P3 passed against prior artifact {artifact_id}",
                candidate_artifact_id=artifact_id,
            ),
            probe,
            artifact_id=artifact_id,
        )
        action, verdict, detail = Action.REUSE, "reuse", "probe-authorized reuse"
    elif resumed:
        decision_id = record_decision(
            pool,
            ReuseDecision(
                work_key=work_key,
                stage=stage_name,
                action=Action.RESUME.value,
                verdict="resumed",
                proposed_by="rule",
                latency_ms=duration_ms,
                explanation=f"resumed from {len(list_fragments(pool, work_key))} recorded fragment(s)",
            ),
        )
        action, verdict, detail = Action.RESUME, "resumed", "resumed from fragments"
    else:
        decision_id = record_decision(
            pool,
            ReuseDecision(
                work_key=work_key,
                stage=stage_name,
                action=Action.RECOMPUTE.value,
                verdict="recompute",
                proposed_by="rule",
                latency_ms=duration_ms,
                explanation="no reuse candidate available or reuse gate failed; real compute",
            ),
        )
        action, verdict, detail = Action.RECOMPUTE, "recompute", "recomputed"

    return StageOutcome(
        stage=stage_name,
        work_key=work_key,
        action=action,
        verdict=verdict,
        artifact=ArtifactHandle(
            artifact_id=artifact_id,
            s3_uri=s3_uri,
            stage=stage_name,
            work_key=work_key,
            env_fingerprint=environment.env_fingerprint,
        ),
        decision_id=decision_id,
        detail=detail,
    )


def _replan_and_recurse(pool: ConnectionPool, **kwargs: Any) -> StageOutcome:
    remediation_note = kwargs.pop("remediation_note")
    emit_event(
        "remediation.applied",
        {"stage": kwargs.get("stage_name"), "note": remediation_note},
        run_id=str(kwargs.get("run_id")) if kwargs.get("run_id") else None,
    )
    outcome = run_stage(pool, **kwargs)
    return StageOutcome(
        stage=outcome.stage,
        work_key=outcome.work_key,
        action=Action.REMEDIATE_AND_REPLAN,
        verdict=outcome.verdict,
        artifact=outcome.artifact,
        decision_id=outcome.decision_id,
        detail=f"{remediation_note}; then {outcome.detail}",
    )


def _act_features(
    pool: ConnectionPool,
    *,
    stage_plan: StagePlan,
    config: TrackedConfig,
    bucket: str,
    owner: str,
    region: str,
    run_id: uuid.UUID,
    fence: int,
    environment: EnvManifest,
    upstream: Mapping[str, StageOutcome],
    resumed: bool,
) -> tuple[str, str, str, int, ProbeResult | None]:
    import pandas as pd
    import pyarrow as pa

    dataset_handle = upstream["dataset"].artifact
    assert dataset_handle is not None
    dataset_bytes = _get_upstream_bytes(bucket, dataset_handle)

    features_config = stage_features.read_config(config.stage("features", record_as="features"))
    model_name = str(features_config["model_name"])
    embedding_dim = int(features_config["embedding_dim"])
    max_seq_length = int(features_config["max_seq_length"])
    batch_size = int(features_config["batch_size"])
    shard_count = int(features_config["shard_count"])

    # --- reuse-via-probe: only when not resuming a takeover, and only
    # when a candidate exists whose upstream dataset artifact is the exact
    # same one this run would use (no data drift the probe can't see). ---
    if not resumed:
        candidate = latest_succeeded_artifact(pool, "features")
        if candidate is not None:
            candidate_inputs = load_artifact_inputs(pool, candidate.artifact_id)
            candidate_dataset_ref = next(
                (i.input_ref for i in candidate_inputs if i.input_kind == "upstream"), None
            )
            if candidate_dataset_ref == dataset_handle.artifact_id:
                p1 = p1_env.run(candidate.env_fingerprint, environment.env_fingerprint)
                if p1.passed:
                    candidate_handle = ArtifactHandle.from_row(candidate)
                    candidate_bytes = _get_upstream_bytes(bucket, candidate_handle)
                    doc_ids, embeddings = stage_features.read_parquet(candidate_bytes)
                    df = pd.read_parquet(pa.BufferReader(dataset_bytes)).sort_values(
                        "doc_id", kind="stable"
                    )
                    texts_by_doc_id = dict(zip(df["doc_id"], df["text"], strict=True))
                    model = stage_features._get_model(max_seq_length, model_name)  # noqa: SLF001

                    def _encode(texts: list[str]) -> npt.NDArray[np.float32]:
                        return np.asarray(
                            model.encode(texts, convert_to_numpy=True, normalize_embeddings=False),
                            dtype=np.float32,
                        )

                    p3 = p3_features.run(
                        candidate.artifact_id, doc_ids, embeddings, texts_by_doc_id, encode=_encode
                    )
                    if p3.passed:
                        return (
                            "reuse_probe",
                            candidate.artifact_id,
                            candidate.s3_uri,
                            len(candidate_bytes),
                            p3,
                        )

    df = (
        pd.read_parquet(pa.BufferReader(dataset_bytes))
        .sort_values("doc_id", kind="stable")
        .reset_index(drop=True)
    )
    doc_ids, embeddings = _persist_features_fragments(
        pool,
        df,
        work_key=stage_plan.work_key.value,
        run_id=run_id,
        fence=fence,
        bucket=bucket,
        shard_count=shard_count,
        batch_size=batch_size,
        max_seq_length=max_seq_length,
        model_name=model_name,
        embedding_dim=embedding_dim,
    )
    parquet_bytes = stage_features._write_parquet(doc_ids, embeddings)  # noqa: SLF001
    put = s3.put_content_addressed(bucket, "features", parquet_bytes)
    return "recompute", put.content_digest, put.s3_uri, len(parquet_bytes), None


def _act_generic(
    pool: ConnectionPool,
    *,
    stage_name: str,
    stage_plan: StagePlan,
    config: TrackedConfig,
    bucket: str,
    run_id: uuid.UUID,
    fence: int,
    environment: EnvManifest,
    upstream: Mapping[str, StageOutcome],
) -> tuple[str, str, int]:
    if stage_name == "env":
        import json

        payload = json.dumps(
            {
                "image_digest": environment.image_digest,
                "python_version": environment.python_version,
                "deps": environment.deps,
                "torch_threads": environment.torch_threads,
            },
            sort_keys=True,
        ).encode("utf-8")
        env_put = s3.put_content_addressed(bucket, "env", payload)
        return env_put.content_digest, env_put.s3_uri, len(payload)

    if stage_name == "dataset":
        snapshot_version = config.get("data.snapshot_digest", stage="__data__", expected_type=str)
        raw_bytes = s3.get_bytes(bucket, f"datasets/{snapshot_version}/raw.parquet")
        dataset_artifact = stage_dataset.run(
            raw_bytes, config=config.stage("dataset", record_as="dataset")
        )
        dataset_put = s3.put_content_addressed(bucket, "dataset", dataset_artifact.parquet_bytes)
        return dataset_put.content_digest, dataset_put.s3_uri, len(dataset_artifact.parquet_bytes)

    if stage_name == "checkpoint":
        dataset_handle = upstream["dataset"].artifact
        features_handle = upstream["features"].artifact
        assert dataset_handle is not None and features_handle is not None
        dataset_bytes = _get_upstream_bytes(bucket, dataset_handle)
        features_bytes = _get_upstream_bytes(bucket, features_handle)
        joined = stage_train.join_dataset_and_features(dataset_bytes, features_bytes)
        train_config = stage_train.read_config(config.stage("train", record_as="checkpoint"))
        checkpoint_artifact = _run_checkpoint_with_fragments(
            pool,
            joined,
            work_key=stage_plan.work_key.value,
            run_id=run_id,
            fence=fence,
            bucket=bucket,
            num_labels=int(train_config["num_labels"]),
            epochs=int(train_config["epochs"]),
            input_dim=int(train_config["input_dim"]),
            hidden_dim=int(train_config["hidden_dim"]),
            learning_rate=float(train_config["learning_rate"]),
            batch_size=int(train_config["batch_size"]),
        )
        checkpoint_put = s3.put_content_addressed(
            bucket, "checkpoint", checkpoint_artifact.state_dict_bytes
        )
        return (
            checkpoint_put.content_digest,
            checkpoint_put.s3_uri,
            len(checkpoint_artifact.state_dict_bytes),
        )

    if stage_name == "eval":
        dataset_handle = upstream["dataset"].artifact
        features_handle = upstream["features"].artifact
        checkpoint_handle = upstream["checkpoint"].artifact
        assert dataset_handle and features_handle and checkpoint_handle
        dataset_bytes = _get_upstream_bytes(bucket, dataset_handle)
        features_bytes = _get_upstream_bytes(bucket, features_handle)
        checkpoint_bytes = _get_upstream_bytes(bucket, checkpoint_handle)
        joined = stage_train.join_dataset_and_features(dataset_bytes, features_bytes)
        # input_dim/hidden_dim/num_labels must match the checkpoint's own
        # training shape, not stage_eval.run's module-level defaults —
        # stage_eval.py's own docstring is explicit that eval has no way
        # to discover the architecture on its own. Read train's config
        # again (record_as="eval": harmless, and makes the dependency this
        # stage's own compute actually has on train.* shape explicit in
        # its config_reads, on top of the indirect protection it already
        # gets via the checkpoint's own artifact_id changing).
        train_shape = stage_train.read_config(config.stage("train", record_as="eval"))
        eval_artifact = stage_eval.run(
            joined,
            checkpoint_bytes,
            config=config.stage("eval", record_as="eval"),
            input_dim=int(train_shape["input_dim"]),
            hidden_dim=int(train_shape["hidden_dim"]),
            num_labels=int(train_shape["num_labels"]),
        )
        eval_put = s3.put_content_addressed(bucket, "eval", eval_artifact.json_bytes)
        return eval_put.content_digest, eval_put.s3_uri, len(eval_artifact.json_bytes)

    raise ValueError(f"unknown stage {stage_name!r}")


def _learn_from_failure(
    pool: ConnectionPool,
    *,
    stage: str,
    run_id: uuid.UUID,
    exc: Exception,
    stage_plan: StagePlan,
    upstream: Mapping[str, StageOutcome],
) -> None:
    """Real learn-from-failure: record a genuine failure signature with the
    real traceback and, if an LLM is available, an unverified remediation
    proposal for next time. Never blocks or re-raises anything of its own
    — the caller's own exception is what propagates."""

    error_class = type(exc).__name__
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    traceback_head = tb.strip().splitlines()[-1] if tb.strip() else f"{error_class}: {exc}"
    summary_text = f"{stage} stage failed with {error_class}: {exc}"
    structured: dict[str, object] = {
        "framework": "pytorch",
        "framework_version": torch.__version__,
        "instance_kind": _INSTANCE_KIND,
        "vcpu": _VCPU,
        "mem_mib": _MEM_MIB,
    }
    for dotted, value in stage_plan.config_reads.items():
        key = dotted.split(".", 1)[1] if "." in dotted else dotted
        if key in ("embedding_dim", "batch_size", "max_seq_length", "num_labels", "learning_rate"):
            mapped = {"max_seq_length": "max_seq_len", "learning_rate": "lr"}.get(key, key)
            structured[mapped] = value

    def _int(key: str) -> int | None:
        value = structured.get(key)
        return int(value) if isinstance(value, int | float) else None

    def _float(key: str) -> float | None:
        value = structured.get(key)
        return float(value) if isinstance(value, int | float) else None

    try:
        embedding = default_provider().embed(summary_text)
        signature_id = memory.record_failure_signature(
            pool,
            memory.FailureSignature(
                stage=stage,
                error_class=error_class,
                workload_kind="text-classification",
                traceback_head=traceback_head[:500],
                summary_text=summary_text,
                embedding=embedding,
                run_id=run_id,
                wasted_ms=0,
                framework=str(structured["framework"]),
                framework_version=str(structured["framework_version"]),
                instance_kind=str(structured["instance_kind"]),
                vcpu=_float("vcpu"),
                mem_mib=_int("mem_mib"),
                embedding_dim=_int("embedding_dim"),
                batch_size=_int("batch_size"),
                max_seq_len=_int("max_seq_len"),
                num_labels=_int("num_labels"),
                lr=_float("lr"),
            ),
        )
        proposal = classify_llm.author_remediation(
            traceback_head=traceback_head[:500], structured=structured
        )
        if proposal is not None:
            memory.record_remediation(
                pool,
                memory.Remediation(
                    signature_id=signature_id,
                    changed_keys=[
                        memory.ChangedKey(key=key, from_value=None, to_value=value)
                        for key, value in proposal.changed_keys
                    ],
                    rationale=proposal.rationale,
                    succeeded=False,
                ),
            )
    except Exception:  # noqa: BLE001 - learning must never mask the real failure
        pass


# ---------------------------------------------------------------------------
# whole-pipeline orchestration — `cairn run <stage> [--all]`
# ---------------------------------------------------------------------------


def run_pipeline(
    pool: ConnectionPool,
    *,
    config: TrackedConfig,
    source_root: str,
    bucket: str,
    owner: str,
    host: str,
    region: str,
    target_stage: str = "eval",
    approval_usd: float = 0.50,
    run_id: uuid.UUID | None = None,
) -> list[StageOutcome]:
    run_id = run_id or uuid.uuid4()
    emit_event(
        "run.started",
        {"target_stage": target_stage, "bucket": bucket, "region": region, "owner": owner},
        run_id=str(run_id),
    )
    environment = stage_env.run()
    ensure_environment(
        pool,
        env_fingerprint=environment.env_fingerprint,
        image_digest=environment.image_digest,
        python_version=environment.python_version,
        deps=environment.deps,
        torch_threads=environment.torch_threads,
    )
    data_fingerprint = config.get("data.snapshot_digest", stage="__data__", expected_type=str)
    plan = plan_pipeline(
        config,
        source_root=source_root,
        env_fingerprint=environment.env_fingerprint,
        data_fingerprint=data_fingerprint,
    )
    names = [spec.name for spec in STAGES]
    if target_stage not in names:
        raise ValueError(f"unknown stage {target_stage!r}, must be one of {names}")
    target_index = names.index(target_stage)

    outcomes: dict[str, StageOutcome] = {}
    for spec in STAGES[: target_index + 1]:
        upstream = {name: outcomes[name] for name in spec.upstream}
        emit_event(
            "stage.started",
            {"stage": spec.name, "work_key": plan.by_stage()[spec.name].work_key.value},
            run_id=str(run_id),
        )
        try:
            outcome = run_stage(
                pool,
                plan=plan,
                stage_name=spec.name,
                config=config,
                bucket=bucket,
                owner=owner,
                host=host,
                region=region,
                run_id=run_id,
                environment=environment,
                upstream=upstream,
                approval_usd=approval_usd,
            )
        except Exception as exc:
            emit_event(
                "stage.failed",
                {"stage": spec.name, "error_class": type(exc).__name__, "error": str(exc)},
                run_id=str(run_id),
            )
            # Quarantine needs direct evidence against an artifact. The old
            # fallback blamed the last reused upstream for *any* exception;
            # a missing independent `datasets/.../raw.parquet` therefore
            # quarantined a healthy env artifact whose S3 object existed.
            # Upstream reads now identify their artifact explicitly; all
            # other failures remain learned negative memory, without a false
            # causal claim.
            if isinstance(exc, UpstreamArtifactUnavailable):
                record_contradiction(
                    pool,
                    exc.artifact_id,
                    run_id,
                    evidence=f"{spec.name} stage could not read {exc}",
                )
            emit_event(
                "run.failed",
                {"target_stage": target_stage, "failed_stage": spec.name},
                run_id=str(run_id),
            )
            raise
        emit_event(
            "stage.completed",
            {
                "stage": spec.name,
                "work_key": outcome.work_key,
                "action": outcome.action.value,
                "verdict": outcome.verdict,
                "artifact_id": outcome.artifact.artifact_id if outcome.artifact else None,
                "detail": outcome.detail,
            },
            run_id=str(run_id),
        )
        outcomes[spec.name] = outcome

    emit_event(
        "run.completed",
        {"target_stage": target_stage, "stages": list(outcomes)},
        run_id=str(run_id),
    )
    return [outcomes[spec.name] for spec in STAGES[: target_index + 1]]
