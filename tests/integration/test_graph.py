from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Jsonb

from cairn.db import claims
from cairn.db.graph import ArtifactInput, load_artifact_inputs, persist_code_graph
from cairn.db.txn import in_txn
from cairn.fingerprint.astcanon import CodeUnit
from cairn.fingerprint.reach import CodeEdge, CodeGraph

pytestmark = pytest.mark.integration


def test_code_graph_population_is_idempotent(pool) -> None:
    token = uuid.uuid4().hex
    commit_sha = f"test-{token}"
    src = f"test.module:{token}"
    dst = f"test.module:{token}_dependency"
    units = {
        unit_id: CodeUnit(
            unit_id=unit_id,
            module="test.module",
            qualname=unit_id.split(":", 1)[1],
            ast_digest=f"digest-{unit_id}",
            docstring_digest="docs",
            is_private=False,
            source_path="test.py",
        )
        for unit_id in (src, dst)
    }
    graph = CodeGraph(
        units=units,
        edges=frozenset({CodeEdge(src, dst, "calls")}),
        escapes={},
    )

    assert persist_code_graph(pool, graph, commit_sha=commit_sha) == (2, 1)
    assert persist_code_graph(pool, graph, commit_sha=commit_sha) == (2, 1)

    def _counts(cur) -> tuple[int, int]:
        cur.execute("SELECT count(*) FROM code_units WHERE commit_sha=%s", (commit_sha,))
        unit_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM code_edges WHERE commit_sha=%s", (commit_sha,))
        edge_count = cur.fetchone()[0]
        return unit_count, edge_count

    assert in_txn(pool, _counts, op="test.code_graph_counts") == (2, 1)


def test_claim_completion_atomically_records_typed_causal_inputs(pool) -> None:
    token = uuid.uuid4().hex
    work_key = f"graph-{token}"
    run_id = uuid.uuid4()
    env_fingerprint = f"env-{token}"

    def _environment(cur) -> None:
        cur.execute(
            """
            INSERT INTO environments
              (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (env_fingerprint, "sha256:test", "3.12", Jsonb({"test": True}), 1),
        )

    in_txn(pool, _environment, op="test.graph_environment")
    claim = claims.acquire(
        pool, work_key, "eval", f"owner-{token}", "localhost", "us-east-1", run_id
    )
    assert claim.won
    artifact = claims.ArtifactRecord(
        artifact_id=f"artifact-{token}",
        stage="eval",
        work_key=work_key,
        s3_uri=f"s3://test/{token}",
        size_bytes=10,
        env_fingerprint=env_fingerprint,
        produced_by_run=run_id,
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    inputs = [
        ArtifactInput("env", "environment", env_fingerprint),
        ArtifactInput("config", "eval.metrics", "digest"),
        ArtifactInput("upstream", "checkpoint-id", "checkpoint-id"),
    ]
    assert claims.complete(pool, work_key, f"owner-{token}", claim.fence, artifact, inputs=inputs)
    assert load_artifact_inputs(pool, artifact.artifact_id) == sorted(inputs)


def test_dispossessed_completion_leaves_no_orphan_artifact_or_inputs(pool) -> None:
    token = uuid.uuid4().hex
    work_key = f"stale-graph-{token}"
    run_id = uuid.uuid4()
    env_fingerprint = f"env-stale-{token}"

    def _environment(cur) -> None:
        cur.execute(
            """
            INSERT INTO environments
              (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (env_fingerprint, "sha256:test", "3.12", Jsonb({"test": True}), 1),
        )

    in_txn(pool, _environment, op="test.stale_graph_environment")
    first = claims.acquire(pool, work_key, "eval", "old-owner", "host", "region", run_id)
    assert first.won

    def _expire(cur) -> None:
        cur.execute(
            "UPDATE work_claims SET lease_expires_at=now() - interval '1 second' WHERE work_key=%s",
            (work_key,),
        )

    in_txn(pool, _expire, op="test.stale_graph_expire")
    second = claims.acquire(pool, work_key, "eval", "new-owner", "host", "region", run_id)
    assert second.won

    artifact = claims.ArtifactRecord(
        artifact_id=f"stale-artifact-{token}",
        stage="eval",
        work_key=work_key,
        s3_uri=f"s3://test/stale/{token}",
        size_bytes=10,
        env_fingerprint=env_fingerprint,
        produced_by_run=run_id,
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    assert not claims.complete(
        pool,
        work_key,
        "old-owner",
        first.fence,
        artifact,
        inputs=[ArtifactInput("config", "eval.metrics", "digest")],
    )

    def _artifact_count(cur) -> int:
        cur.execute("SELECT count(*) FROM artifacts WHERE artifact_id=%s", (artifact.artifact_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]

    assert in_txn(pool, _artifact_count, op="test.stale_graph_count") == 0
    assert load_artifact_inputs(pool, artifact.artifact_id) == []
