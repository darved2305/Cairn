"""Transactional persistence for Cairn's causal artifact/code graph."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.fingerprint.reach import CodeGraph

INPUT_KINDS = frozenset({"code", "config", "data", "upstream", "env"})


class ArtifactLike(Protocol):
    @property
    def artifact_id(self) -> str: ...

    @property
    def stage(self) -> str: ...

    @property
    def work_key(self) -> str: ...

    @property
    def s3_uri(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def env_fingerprint(self) -> str: ...

    @property
    def produced_by_run(self) -> UUID: ...

    @property
    def duration_ms(self) -> int: ...

    @property
    def vcpu(self) -> float: ...

    @property
    def mem_mib(self) -> int: ...

    @property
    def region(self) -> str: ...


@dataclass(frozen=True, order=True)
class ArtifactInput:
    input_kind: str
    input_ref: str
    input_digest: str

    def __post_init__(self) -> None:
        if self.input_kind not in INPUT_KINDS:
            raise ValueError(
                f"input_kind must be one of {sorted(INPUT_KINDS)}, got {self.input_kind!r}"
            )
        if not self.input_ref:
            raise ValueError("input_ref must be non-empty")
        if not self.input_digest:
            raise ValueError("input_digest must be non-empty")


class CausalInputConflict(RuntimeError):
    """One content-addressed artifact was assigned contradictory provenance."""


def insert_artifact(
    cur: psycopg.Cursor,
    artifact: ArtifactLike,
    inputs: Sequence[ArtifactInput],
    *,
    allow_existing: bool = False,
) -> None:
    """Insert an artifact and its typed inputs inside the caller's transaction.

    `allow_existing` implements docs/project/PROJECT.md's completion idempotency: two
    causally distinct work keys may deterministically produce the same bytes,
    and therefore the same content-addressed artifact. The first insertion is
    the artifact's canonical provenance; later claims converge on that row
    rather than trying to overwrite it with a second input set the schema
    cannot represent. Direct graph writes remain strict by default.
    """

    _validate_inputs(inputs)
    cur.execute(
        """
        INSERT INTO artifacts
          (artifact_id, stage, work_key, s3_uri, size_bytes, env_fingerprint,
           produced_by_run, duration_ms, vcpu, mem_mib, region)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (artifact_id) DO NOTHING
        RETURNING artifact_id
        """,
        (
            artifact.artifact_id,
            artifact.stage,
            artifact.work_key,
            artifact.s3_uri,
            artifact.size_bytes,
            artifact.env_fingerprint,
            artifact.produced_by_run,
            artifact.duration_ms,
            artifact.vcpu,
            artifact.mem_mib,
            artifact.region,
        ),
    )
    inserted = cur.fetchone() is not None
    if not inserted and allow_existing:
        return
    for item in sorted(inputs):
        cur.execute(
            """
            INSERT INTO artifact_inputs
              (artifact_id, input_kind, input_ref, input_digest)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (artifact_id, input_kind, input_ref)
            DO UPDATE SET input_digest=artifact_inputs.input_digest
              WHERE artifact_inputs.input_digest=excluded.input_digest
            RETURNING input_digest
            """,
            (artifact.artifact_id, item.input_kind, item.input_ref, item.input_digest),
        )
        row = cur.fetchone()
        if row is None:
            raise CausalInputConflict(
                f"artifact {artifact.artifact_id!r} already has a different digest for "
                f"{item.input_kind}:{item.input_ref}"
            )


def record_artifact(
    pool: ConnectionPool, artifact: ArtifactLike, inputs: Sequence[ArtifactInput]
) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        insert_artifact(cur, artifact, inputs)

    in_txn(pool, _tx, op="graph.record_artifact")


def persist_code_graph(
    pool: ConnectionPool, graph: CodeGraph, *, commit_sha: str
) -> tuple[int, int]:
    """Idempotently store the current snapshot of units and edges."""

    if not commit_sha:
        raise ValueError("commit_sha must be non-empty")

    def _tx(cur: psycopg.Cursor) -> tuple[int, int]:
        cur.execute("DELETE FROM code_edges WHERE commit_sha=%s RETURNING NOTHING", (commit_sha,))
        unit_rows = [
            (
                unit.unit_id,
                unit.module,
                unit.qualname,
                unit.ast_digest,
                unit.docstring_digest,
                unit.is_private,
                commit_sha,
            )
            for unit in sorted(graph.units.values(), key=lambda value: value.unit_id)
        ]
        if unit_rows:
            # psycopg 3 pipelines executemany() instead of waiting for a
            # CockroachDB round trip after every code unit.  The previous
            # loop held this SERIALIZABLE transaction open for minutes on a
            # real cloud cluster (hundreds of sequential statements).
            cur.executemany(
                """
                INSERT INTO code_units
                  (unit_id, module, qualname, ast_digest, docstring_digest,
                   is_private, commit_sha)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (unit_id) DO UPDATE SET
                  module=excluded.module,
                  qualname=excluded.qualname,
                  ast_digest=excluded.ast_digest,
                  docstring_digest=excluded.docstring_digest,
                  is_private=excluded.is_private,
                  commit_sha=excluded.commit_sha
                RETURNING NOTHING
                """,
                unit_rows,
            )

        edge_rows = [
            (commit_sha, edge.src_unit, edge.dst_unit, edge.edge_kind)
            for edge in sorted(graph.edges)
        ]
        if edge_rows:
            cur.executemany(
                """
                INSERT INTO code_edges
                  (commit_sha, src_unit, dst_unit, edge_kind)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (commit_sha, src_unit, dst_unit, edge_kind) DO NOTHING
                RETURNING NOTHING
                """,
                edge_rows,
            )
        return len(graph.units), len(graph.edges)

    return in_txn(pool, _tx, op="graph.persist_code_graph")


@dataclass(frozen=True)
class ArtifactRow:
    artifact_id: str
    stage: str
    work_key: str
    s3_uri: str
    env_fingerprint: str


def get_artifact(pool: ConnectionPool, artifact_id: str) -> ArtifactRow | None:
    """One artifact's identifying columns, by id — used by agent/loop.py to
    resolve a `claims.acquire` reuse hit or a `subscribe` adoption into an
    S3 location without duplicating the query at every call site."""

    def _tx(cur: psycopg.Cursor) -> ArtifactRow | None:
        cur.execute(
            "SELECT artifact_id, stage, work_key, s3_uri, env_fingerprint "
            "FROM artifacts WHERE artifact_id=%s",
            (artifact_id,),
        )
        row = cur.fetchone()
        return ArtifactRow(*row) if row else None

    return in_txn(pool, _tx, op="graph.get_artifact")


def latest_succeeded_artifact(pool: ConnectionPool, stage: str) -> ArtifactRow | None:
    """The most recent, non-quarantined artifact for a stage, regardless of
    work_key — the reuse-via-probe candidate agent/loop.py evaluates when
    an exact work_key match (identity reuse) isn't available."""

    def _tx(cur: psycopg.Cursor) -> ArtifactRow | None:
        cur.execute(
            """
            SELECT artifact_id, stage, work_key, s3_uri, env_fingerprint
              FROM artifacts
             WHERE stage=%s AND quarantined_at IS NULL
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (stage,),
        )
        row = cur.fetchone()
        return ArtifactRow(*row) if row else None

    return in_txn(pool, _tx, op="graph.latest_succeeded_artifact")


def load_artifact_inputs(pool: ConnectionPool, artifact_id: str) -> list[ArtifactInput]:
    def _tx(cur: psycopg.Cursor) -> list[ArtifactInput]:
        cur.execute(
            """
            SELECT input_kind, input_ref, input_digest
              FROM artifact_inputs
             WHERE artifact_id=%s
             ORDER BY input_kind, input_ref
            """,
            (artifact_id,),
        )
        return [ArtifactInput(*row) for row in cur.fetchall()]

    return in_txn(pool, _tx, op="graph.load_artifact_inputs")


@dataclass(frozen=True)
class ArtifactDetails:
    """Everything `cairn explain <artifact_id>` and the TUI's Explain panel
    need in one read — the full artifacts row plus its typed input edges,
    so callers don't have to compose get_artifact + load_artifact_inputs
    themselves for what is always used together."""

    artifact_id: str
    stage: str
    work_key: str
    s3_uri: str
    size_bytes: int
    env_fingerprint: str
    produced_by_run: UUID
    duration_ms: int
    vcpu: float
    mem_mib: int
    region: str
    quarantined_at: datetime | None
    created_at: datetime
    inputs: list[ArtifactInput]


def describe_artifact(pool: ConnectionPool, artifact_id: str) -> ArtifactDetails | None:
    def _tx(cur: psycopg.Cursor) -> ArtifactDetails | None:
        cur.execute(
            """
            SELECT artifact_id, stage, work_key, s3_uri, size_bytes, env_fingerprint,
                   produced_by_run, duration_ms, vcpu, mem_mib, region,
                   quarantined_at, created_at
              FROM artifacts WHERE artifact_id=%s
            """,
            (artifact_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            """
            SELECT input_kind, input_ref, input_digest
              FROM artifact_inputs
             WHERE artifact_id=%s
             ORDER BY input_kind, input_ref
            """,
            (artifact_id,),
        )
        inputs = [ArtifactInput(*input_row) for input_row in cur.fetchall()]
        (
            aid,
            stage,
            work_key,
            s3_uri,
            size_bytes,
            env_fingerprint,
            produced_by_run,
            duration_ms,
            vcpu,
            mem_mib,
            region,
            quarantined_at,
            created_at,
        ) = row
        return ArtifactDetails(
            artifact_id=aid,
            stage=stage,
            work_key=work_key,
            s3_uri=s3_uri,
            size_bytes=size_bytes,
            env_fingerprint=env_fingerprint,
            produced_by_run=produced_by_run,
            duration_ms=duration_ms,
            vcpu=float(vcpu),
            mem_mib=mem_mib,
            region=region,
            quarantined_at=quarantined_at,
            created_at=created_at,
            inputs=inputs,
        )

    return in_txn(pool, _tx, op="graph.describe_artifact")


def downstream_artifacts(pool: ConnectionPool, artifact_id: str) -> list[str]:
    """Artifacts whose `artifact_inputs` cites `artifact_id` directly as an
    'upstream' input — one hop, matching db/contradictions.py's
    `_downstream_of` (the same edge shape); the CLI/TUI explain surface
    shows direct dependents, not a transitive closure."""

    def _tx(cur: psycopg.Cursor) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT artifact_id FROM artifact_inputs
             WHERE input_kind = 'upstream' AND input_ref = %s
             ORDER BY artifact_id
            """,
            (artifact_id,),
        )
        return [row[0] for row in cur.fetchall()]

    return in_txn(pool, _tx, op="graph.downstream_artifacts")


def _validate_inputs(inputs: Sequence[ArtifactInput]) -> None:
    identities = [(item.input_kind, item.input_ref) for item in inputs]
    if len(identities) != len(set(identities)):
        raise ValueError("artifact inputs contain a duplicate (input_kind, input_ref)")
