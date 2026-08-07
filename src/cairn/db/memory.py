"""Tiered negative computational memory — PROJECT.md §4.1, D6.

Vector similarity alone must never gate execution: `tier()` implements the
exact/strong_semantic/weak table exactly as specified, and `weak` is
advisory-only by construction — there is no code path in this module that
returns a blocking verdict for it. `exact` and `strong_semantic` both
additionally require a *recorded, succeeded remediation* to know which
structured features were actually causal for a given signature; absent
that provenance, cosine distance alone can never elevate a match past
`weak`, which mirrors conservative mode's own rule (no evidence -> the
safe outcome).

Thresholds live in one module-level dict, `TIERS`, so they are tunable
and testable in one place — PLAN.md §3.4.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn

TIERS = {
    "strong_max_cosine": 0.15,
    "weak_max_cosine": 0.35,
}

# failure_signatures' structured, exact-matchable columns — PROJECT.md §4.1.
STRUCTURED_FIELDS = (
    "model_family",
    "model_id",
    "embedding_dim",
    "num_labels",
    "dataset_rows",
    "max_seq_len",
    "batch_size",
    "grad_accum",
    "precision",
    "optimizer",
    "lr",
    "instance_kind",
    "vcpu",
    "mem_mib",
    "accelerator",
    "framework",
    "framework_version",
    "oom_killed",
)


class MatchTier(StrEnum):
    EXACT = "exact"
    STRONG_SEMANTIC = "strong_semantic"
    WEAK = "weak"
    NONE = "none"


# The only tiers that are ever allowed to refuse a plan. Anything not in
# this set — WEAK, NONE — must never block; enforced by construction in
# agent/loop.py's caller, and proven never to regress by
# tests/unit/test_memory.py::test_weak_and_none_never_block.
BLOCKING_TIERS = frozenset({MatchTier.EXACT, MatchTier.STRONG_SEMANTIC})


@dataclass(frozen=True)
class PlanFeatures:
    """The structured feature vector for the *proposed* run — PROJECT.md
    §4.1 step 1."""

    stage: str
    error_class_hint: str | None
    structured: Mapping[str, object]


@dataclass(frozen=True)
class Match:
    signature_id: uuid.UUID
    stage: str
    error_class: str
    traceback_head: str
    summary_text: str
    structured: Mapping[str, object]
    wasted_ms: int
    created_at: datetime
    cosine_distance: float
    # Keys from the most recent *succeeded* remediation's changed_keys —
    # None when no remediation is on record for this signature yet.
    causal_keys: frozenset[str] | None


@dataclass(frozen=True)
class TieredMatch:
    match: Match
    tier: MatchTier
    agreeing_keys: frozenset[str]


def tier(match: Match, candidate: PlanFeatures) -> TieredMatch:
    agreeing = frozenset(
        key
        for key in STRUCTURED_FIELDS
        if match.structured.get(key) is not None
        and match.structured.get(key) == candidate.structured.get(key)
    )
    causal = match.causal_keys

    if (
        causal is not None
        and causal <= agreeing
        and match.error_class == candidate.error_class_hint
        and match.structured.get("framework_version")
        == candidate.structured.get("framework_version")
        and match.structured.get("instance_kind") == candidate.structured.get("instance_kind")
    ):
        return TieredMatch(match, MatchTier.EXACT, agreeing)

    if (
        causal is not None
        and match.cosine_distance <= TIERS["strong_max_cosine"]
        and match.stage == candidate.stage
        and match.error_class == candidate.error_class_hint
        and causal & agreeing
    ):
        return TieredMatch(match, MatchTier.STRONG_SEMANTIC, agreeing)

    if match.cosine_distance <= TIERS["weak_max_cosine"]:
        return TieredMatch(match, MatchTier.WEAK, agreeing)

    return TieredMatch(match, MatchTier.NONE, agreeing)


def _vector_literal(values: Sequence[float]) -> str:
    """CockroachDB's VECTOR parser wants `"[v1,v2,...]"` — psycopg's default
    Python-list adapter produces a Postgres ARRAY literal instead, which the
    VECTOR parser rejects outright (proven empirically, not assumed)."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _extract_causal_keys(
    changed_keys: Sequence[Mapping[str, object]] | None,
) -> frozenset[str] | None:
    if not changed_keys:
        return None
    return frozenset(str(entry["key"]) for entry in changed_keys)


def _row_to_match(row: tuple[object, ...]) -> Match:
    (
        signature_id,
        stage,
        error_class,
        traceback_head,
        summary_text,
        model_family,
        model_id,
        embedding_dim,
        num_labels,
        dataset_rows,
        max_seq_len,
        batch_size,
        grad_accum,
        precision,
        optimizer,
        lr,
        instance_kind,
        vcpu,
        mem_mib,
        accelerator,
        framework,
        framework_version,
        oom_killed,
        wasted_ms,
        created_at,
        dist,
        changed_keys,
    ) = row
    structured = {
        "model_family": model_family,
        "model_id": model_id,
        "embedding_dim": embedding_dim,
        "num_labels": num_labels,
        "dataset_rows": dataset_rows,
        "max_seq_len": max_seq_len,
        "batch_size": batch_size,
        "grad_accum": grad_accum,
        "precision": precision,
        "optimizer": optimizer,
        "lr": lr,
        "instance_kind": instance_kind,
        "vcpu": vcpu,
        "mem_mib": mem_mib,
        "accelerator": accelerator,
        "framework": framework,
        "framework_version": framework_version,
        "oom_killed": oom_killed,
    }
    return Match(
        signature_id=signature_id,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        error_class=error_class,  # type: ignore[arg-type]
        traceback_head=traceback_head,  # type: ignore[arg-type]
        summary_text=summary_text,  # type: ignore[arg-type]
        structured=structured,
        wasted_ms=wasted_ms,  # type: ignore[arg-type]
        created_at=created_at,  # type: ignore[arg-type]
        cosine_distance=float(dist),  # type: ignore[arg-type]
        causal_keys=_extract_causal_keys(
            cast("Sequence[Mapping[str, object]] | None", changed_keys)
        ),
    )


def search(
    pool: ConnectionPool,
    *,
    stage: str,
    error_class_hint: str | None,
    embedding: Sequence[float],
    limit: int = 8,
) -> list[Match]:
    """Structured pre-filter + vector search, joined against each
    signature's most recent succeeded remediation in the same query —
    PROJECT.md §4.2's "one store, one transaction, no dual-write" rationale
    for keeping this in CockroachDB rather than a separate vector DB."""

    vector = _vector_literal(embedding)

    def _tx(cur: psycopg.Cursor) -> list[Match]:
        cur.execute(
            """
            SELECT fs.signature_id, fs.stage, fs.error_class, fs.traceback_head, fs.summary_text,
                   fs.model_family, fs.model_id, fs.embedding_dim, fs.num_labels, fs.dataset_rows,
                   fs.max_seq_len, fs.batch_size, fs.grad_accum, fs.precision, fs.optimizer, fs.lr,
                   fs.instance_kind, fs.vcpu, fs.mem_mib, fs.accelerator, fs.framework,
                   fs.framework_version, fs.oom_killed, fs.wasted_ms, fs.created_at,
                   fs.embedding <=> %s AS dist,
                   r.changed_keys
              FROM failure_signatures fs
              LEFT JOIN LATERAL (
                     SELECT changed_keys FROM remediations
                      WHERE remediations.signature_id = fs.signature_id AND succeeded
                      ORDER BY created_at DESC LIMIT 1
                   ) r ON true
             WHERE fs.stage = %s
               AND (%s::STRING IS NULL OR fs.error_class = %s)
             ORDER BY fs.embedding <=> %s
             LIMIT %s
            """,
            (vector, stage, error_class_hint, error_class_hint, vector, limit),
        )
        return [_row_to_match(row) for row in cur.fetchall()]

    return in_txn(pool, _tx, op="memory.search")


def search_text(
    pool: ConnectionPool,
    *,
    embedding: Sequence[float],
    stage: str | None = None,
    limit: int = 8,
) -> list[Match]:
    """Stage-optional variant of `search` for `cairn memory search` / the
    TUI's `/memory` command: a human exploring memory doesn't necessarily
    know which stage a failure belongs to, unlike the always-stage-scoped
    pre-flight check in `agent/loop.py`. Same LATERAL join against each
    signature's most recent succeeded remediation as `search`."""

    vector = _vector_literal(embedding)

    def _tx(cur: psycopg.Cursor) -> list[Match]:
        cur.execute(
            """
            SELECT fs.signature_id, fs.stage, fs.error_class, fs.traceback_head, fs.summary_text,
                   fs.model_family, fs.model_id, fs.embedding_dim, fs.num_labels, fs.dataset_rows,
                   fs.max_seq_len, fs.batch_size, fs.grad_accum, fs.precision, fs.optimizer, fs.lr,
                   fs.instance_kind, fs.vcpu, fs.mem_mib, fs.accelerator, fs.framework,
                   fs.framework_version, fs.oom_killed, fs.wasted_ms, fs.created_at,
                   fs.embedding <=> %s AS dist,
                   r.changed_keys
              FROM failure_signatures fs
              LEFT JOIN LATERAL (
                     SELECT changed_keys FROM remediations
                      WHERE remediations.signature_id = fs.signature_id AND succeeded
                      ORDER BY created_at DESC LIMIT 1
                   ) r ON true
             WHERE (%s::STRING IS NULL OR fs.stage = %s)
             ORDER BY fs.embedding <=> %s
             LIMIT %s
            """,
            (vector, stage, stage, vector, limit),
        )
        return [_row_to_match(row) for row in cur.fetchall()]

    return in_txn(pool, _tx, op="memory.search_text")


def vector_index_status(pool: ConnectionPool) -> VectorIndexStatus:
    """Read-only report of whether `fs_sem` exists — unlike
    `ensure_vector_index`, this never runs `SET CLUSTER SETTING` or
    `CREATE INDEX`. `cairn doctor` and the TUI status surface must be able
    to report index health without a side effect; `ensure_vector_index`
    stays the only mutating path, called only from setup scripts."""

    def _tx(cur: psycopg.Cursor) -> VectorIndexStatus:
        cur.execute("SELECT DISTINCT index_name FROM [SHOW INDEXES FROM failure_signatures]")
        names = {row[0] for row in cur.fetchall()}
        if "fs_sem" in names:
            return VectorIndexStatus(active=True, detail="vector index fs_sem present")
        return VectorIndexStatus(
            active=False,
            detail="vector index fs_sem not found — search() falls back to brute-force cosine",
        )

    return in_txn(pool, _tx, op="memory.vector_index_status")


@dataclass(frozen=True)
class FailureSignature:
    stage: str
    error_class: str
    workload_kind: str
    traceback_head: str
    summary_text: str
    embedding: Sequence[float]
    run_id: uuid.UUID
    wasted_ms: int
    model_family: str | None = None
    model_id: str | None = None
    embedding_dim: int | None = None
    num_labels: int | None = None
    dataset_rows: int | None = None
    max_seq_len: int | None = None
    batch_size: int | None = None
    grad_accum: int | None = None
    precision: str | None = None
    optimizer: str | None = None
    lr: float | None = None
    instance_kind: str | None = None
    vcpu: float | None = None
    mem_mib: int | None = None
    accelerator: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    error_module: str | None = None
    exit_code: int | None = None
    oom_killed: bool = False


def record_failure_signature(pool: ConnectionPool, signature: FailureSignature) -> uuid.UUID:
    """Vector rows are written one at a time, per the performance skill's
    guidance recorded in docs/SKILLS_USAGE.md — large batch inserts of
    VECTOR degrade performance, so this is deliberately not batchable."""

    vector = _vector_literal(signature.embedding)

    def _tx(cur: psycopg.Cursor) -> uuid.UUID:
        signature_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO failure_signatures
              (signature_id, stage, error_class, workload_kind, model_family, model_id,
               embedding_dim, num_labels, dataset_rows, max_seq_len, batch_size, grad_accum,
               precision, optimizer, lr, instance_kind, vcpu, mem_mib, accelerator,
               framework, framework_version, error_module, exit_code, oom_killed,
               traceback_head, summary_text, embedding, run_id, wasted_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                signature_id,
                signature.stage,
                signature.error_class,
                signature.workload_kind,
                signature.model_family,
                signature.model_id,
                signature.embedding_dim,
                signature.num_labels,
                signature.dataset_rows,
                signature.max_seq_len,
                signature.batch_size,
                signature.grad_accum,
                signature.precision,
                signature.optimizer,
                signature.lr,
                signature.instance_kind,
                signature.vcpu,
                signature.mem_mib,
                signature.accelerator,
                signature.framework,
                signature.framework_version,
                signature.error_module,
                signature.exit_code,
                signature.oom_killed,
                signature.traceback_head,
                signature.summary_text,
                vector,
                signature.run_id,
                signature.wasted_ms,
            ),
        )
        return signature_id

    return in_txn(pool, _tx, op="memory.record_failure_signature")


@dataclass(frozen=True)
class ChangedKey:
    key: str
    from_value: object
    to_value: object


@dataclass(frozen=True)
class Remediation:
    signature_id: uuid.UUID
    changed_keys: Sequence[ChangedKey]
    rationale: str
    verified_run_id: uuid.UUID | None = None
    succeeded: bool = False


def record_remediation(pool: ConnectionPool, remediation: Remediation) -> uuid.UUID:
    payload = [
        {"key": ck.key, "from": ck.from_value, "to": ck.to_value} for ck in remediation.changed_keys
    ]

    def _tx(cur: psycopg.Cursor) -> uuid.UUID:
        remediation_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO remediations
              (remediation_id, signature_id, changed_keys, rationale, verified_run_id, succeeded)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                remediation_id,
                remediation.signature_id,
                Jsonb(payload),
                remediation.rationale,
                remediation.verified_run_id,
                remediation.succeeded,
            ),
        )
        return remediation_id

    return in_txn(pool, _tx, op="memory.record_remediation")


def latest_successful_remediation(
    pool: ConnectionPool, signature_id: uuid.UUID
) -> Remediation | None:
    """The full remediation row (with actual from/to values), not just the
    `causal_keys` frozenset `tier()`'s `Match` carries — REMEDIATE_AND_REPLAN
    needs the real `to_value`s to mutate a config, not merely the key names."""

    def _tx(cur: psycopg.Cursor) -> Remediation | None:
        cur.execute(
            """
            SELECT changed_keys, rationale, verified_run_id, succeeded
              FROM remediations
             WHERE signature_id = %s AND succeeded
             ORDER BY created_at DESC LIMIT 1
            """,
            (signature_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        changed_keys_json, rationale, verified_run_id, succeeded = row
        changed_keys = [
            ChangedKey(key=entry["key"], from_value=entry["from"], to_value=entry["to"])
            for entry in changed_keys_json
        ]
        return Remediation(
            signature_id=signature_id,
            changed_keys=changed_keys,
            rationale=rationale,
            verified_run_id=verified_run_id,
            succeeded=succeeded,
        )

    return in_txn(pool, _tx, op="memory.latest_successful_remediation")


@dataclass(frozen=True)
class VectorIndexStatus:
    active: bool
    detail: str


def ensure_vector_index(pool: ConnectionPool) -> VectorIndexStatus:
    """Attempt to create the C-SPANN vector index; report which path is
    active rather than raising. PLAN.md §13: index availability varies by
    CockroachDB plan/build — `cairn doctor` surfaces this status, and
    db/memory.py::search runs correct (if slower) brute-force `<=>` queries
    either way, so a missing index is degraded performance, never a
    correctness gap.

    Deliberately not routed through `in_txn`: a real CockroachDB Cloud
    cluster rejects `SET CLUSTER SETTING` inside an explicit transaction
    ("cannot be used inside a multi-statement transaction") — a cluster
    setting is cluster-wide, not scoped to one SQL transaction, and
    `in_txn` always opens one. The single-node local dev image tolerated
    it (masking the bug there; it failed on the *next* statement instead,
    for an unrelated reason), which is exactly why this was only caught
    once this ran against the real cluster. Each statement here commits
    on its own via autocommit, same as scripts/migrate.py's DDL.
    """

    try:
        with pool.connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
                cur.execute(
                    "CREATE VECTOR INDEX fs_sem ON failure_signatures "
                    "(stage, error_class, embedding vector_cosine_ops)"
                )
        return VectorIndexStatus(active=True, detail="vector index fs_sem created")
    except psycopg.errors.DuplicateObject:
        return VectorIndexStatus(active=True, detail="vector index fs_sem already exists")
    except psycopg.Error as exc:
        return VectorIndexStatus(
            active=False,
            detail=f"vector index unavailable, using brute-force cosine fallback: {exc}",
        )
