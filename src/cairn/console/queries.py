"""Read-only queries backing the console API — PLAN.md D8.

Every function here is a plain SELECT; nothing in this module writes.
That's a code-level guarantee today, matching PLAN.md §8 open decision 4's
"write mutations disabled at the IAM/role layer, not just in the UI" —
the DB-role-level enforcement (a read-only Postgres role for the console's
connection string) is a deployment-time follow-up, not a gap in this code.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn

# The five fixed pipeline stages — PROJECT.md §4.3's DAG (env -> dataset ->
# features -> checkpoint -> eval), matching planner.py's STAGES order.
PIPELINE_STAGES = ("env", "dataset", "features", "checkpoint", "eval")


@dataclass(frozen=True)
class DecisionSummary:
    decision_id: uuid.UUID
    work_key: str
    stage: str
    action: str
    verdict: str
    change_class: str | None
    proposed_by: str
    authorized_by: str | None
    latency_ms: int
    explanation: str
    created_at: datetime


@dataclass(frozen=True)
class ArtifactSummary:
    artifact_id: str
    stage: str
    work_key: str
    s3_uri: str
    size_bytes: int
    duration_ms: int
    region: str
    quarantined_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class ProbeRunSummary:
    probe_run_id: uuid.UUID
    probe_type: str
    sample_spec: str
    population_size: int
    sample_size: int
    tolerance: str
    runtime_ms: int
    passed: bool
    evidence_digest: str
    detail: str


@dataclass(frozen=True)
class ArtifactInputEdge:
    input_kind: str
    input_ref: str
    input_digest: str


@dataclass(frozen=True)
class StageStatus:
    stage: str
    latest_decision: DecisionSummary | None
    latest_artifact: ArtifactSummary | None


@dataclass(frozen=True)
class DecisionDetail:
    decision: DecisionSummary
    probe: ProbeRunSummary | None
    artifact_inputs: list[ArtifactInputEdge]


_DECISION_COLUMNS = """
    decision_id, work_key, stage, action, verdict, change_class,
    proposed_by, authorized_by, latency_ms, explanation, created_at
"""


def _row_to_decision(row: tuple[object, ...]) -> DecisionSummary:
    (
        decision_id,
        work_key,
        stage,
        action,
        verdict,
        change_class,
        proposed_by,
        authorized_by,
        latency_ms,
        explanation,
        created_at,
    ) = row
    return DecisionSummary(
        decision_id=decision_id,  # type: ignore[arg-type]
        work_key=work_key,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        verdict=verdict,  # type: ignore[arg-type]
        change_class=change_class,  # type: ignore[arg-type]
        proposed_by=proposed_by,  # type: ignore[arg-type]
        authorized_by=authorized_by,  # type: ignore[arg-type]
        latency_ms=latency_ms,  # type: ignore[arg-type]
        explanation=explanation,  # type: ignore[arg-type]
        created_at=created_at,  # type: ignore[arg-type]
    )


def _row_to_artifact(row: tuple[object, ...]) -> ArtifactSummary:
    (
        artifact_id,
        stage,
        work_key,
        s3_uri,
        size_bytes,
        duration_ms,
        region,
        quarantined_at,
        created_at,
    ) = row
    return ArtifactSummary(
        artifact_id=artifact_id,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        work_key=work_key,  # type: ignore[arg-type]
        s3_uri=s3_uri,  # type: ignore[arg-type]
        size_bytes=size_bytes,  # type: ignore[arg-type]
        duration_ms=duration_ms,  # type: ignore[arg-type]
        region=region,  # type: ignore[arg-type]
        quarantined_at=quarantined_at,  # type: ignore[arg-type]
        created_at=created_at,  # type: ignore[arg-type]
    )


def _row_to_probe(row: tuple[object, ...]) -> ProbeRunSummary:
    (
        probe_run_id,
        probe_type,
        sample_spec,
        population_size,
        sample_size,
        tolerance,
        runtime_ms,
        passed,
        evidence_digest,
        detail,
    ) = row
    return ProbeRunSummary(
        probe_run_id=probe_run_id,  # type: ignore[arg-type]
        probe_type=probe_type,  # type: ignore[arg-type]
        sample_spec=sample_spec,  # type: ignore[arg-type]
        population_size=population_size,  # type: ignore[arg-type]
        sample_size=sample_size,  # type: ignore[arg-type]
        tolerance=tolerance,  # type: ignore[arg-type]
        runtime_ms=runtime_ms,  # type: ignore[arg-type]
        passed=passed,  # type: ignore[arg-type]
        evidence_digest=evidence_digest,  # type: ignore[arg-type]
        detail=detail,  # type: ignore[arg-type]
    )


def pipeline_status(pool: ConnectionPool) -> list[StageStatus]:
    """One row per fixed pipeline stage: its most recent decision (if any)
    and most recent artifact (if any) — independent lookups, because a
    stage can have an artifact from claims.complete() without ever having
    gone through db/decisions.py, and vice versa is possible too."""

    def _tx(cur: psycopg.Cursor) -> list[StageStatus]:
        statuses = []
        for stage in PIPELINE_STAGES:
            cur.execute(
                f"""
                SELECT {_DECISION_COLUMNS} FROM reuse_decisions
                 WHERE stage = %s ORDER BY created_at DESC LIMIT 1
                """,
                (stage,),
            )
            decision_row = cur.fetchone()
            decision = _row_to_decision(decision_row) if decision_row else None

            cur.execute(
                """
                SELECT artifact_id, stage, work_key, s3_uri, size_bytes,
                       duration_ms, region, quarantined_at, created_at
                  FROM artifacts
                 WHERE stage = %s ORDER BY created_at DESC LIMIT 1
                """,
                (stage,),
            )
            artifact_row = cur.fetchone()
            artifact = _row_to_artifact(artifact_row) if artifact_row else None

            statuses.append(
                StageStatus(stage=stage, latest_decision=decision, latest_artifact=artifact)
            )
        return statuses

    return in_txn(pool, _tx, op="console.pipeline_status")


def list_decisions(
    pool: ConnectionPool, *, limit: int = 50, offset: int = 0
) -> tuple[list[DecisionSummary], int]:
    def _tx(cur: psycopg.Cursor) -> tuple[list[DecisionSummary], int]:
        cur.execute(
            f"""
            SELECT {_DECISION_COLUMNS} FROM reuse_decisions
             ORDER BY created_at DESC
             LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        decisions = [_row_to_decision(row) for row in cur.fetchall()]
        cur.execute("SELECT count(*) FROM reuse_decisions")
        row = cur.fetchone()
        assert row is not None
        total = int(row[0])
        return decisions, total

    return in_txn(pool, _tx, op="console.list_decisions")


def decision_detail(pool: ConnectionPool, decision_id: uuid.UUID) -> DecisionDetail | None:
    def _tx(cur: psycopg.Cursor) -> DecisionDetail | None:
        cur.execute(
            f"SELECT {_DECISION_COLUMNS}, probe_run_id, candidate_artifact_id FROM reuse_decisions WHERE decision_id = %s",
            (decision_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        *decision_cols, probe_run_id, candidate_artifact_id = row
        decision = _row_to_decision(tuple(decision_cols))

        probe: ProbeRunSummary | None = None
        if probe_run_id is not None:
            cur.execute(
                """
                SELECT probe_run_id, probe_type, sample_spec, population_size,
                       sample_size, tolerance, runtime_ms, passed, evidence_digest, detail
                  FROM probe_runs WHERE probe_run_id = %s
                """,
                (probe_run_id,),
            )
            probe_row = cur.fetchone()
            probe = _row_to_probe(probe_row) if probe_row else None

        edges: list[ArtifactInputEdge] = []
        if candidate_artifact_id is not None:
            cur.execute(
                """
                SELECT input_kind, input_ref, input_digest FROM artifact_inputs
                 WHERE artifact_id = %s ORDER BY input_kind, input_ref
                """,
                (candidate_artifact_id,),
            )
            edges = [ArtifactInputEdge(*row) for row in cur.fetchall()]

        return DecisionDetail(decision=decision, probe=probe, artifact_inputs=edges)

    return in_txn(pool, _tx, op="console.decision_detail")
