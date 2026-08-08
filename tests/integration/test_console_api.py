"""Integration tests for the read-only console API — PLAN.md D8.

Every test seeds real rows in the live CockroachDB cluster (no mocked DB,
PLAN.md §5's anti-simulation rule) and asserts against the FastAPI app's
actual JSON responses via TestClient.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from cairn.console.api import app
from cairn.db import claims
from cairn.db.decisions import ReuseDecision, record_decision, record_decision_with_probe
from cairn.db.graph import ArtifactInput, record_artifact
from cairn.db.txn import in_txn
from cairn.probes.base import ProbeResult

pytestmark = pytest.mark.integration


def _probe_result(*, passed: bool = True) -> ProbeResult:
    return ProbeResult(
        probe_type="P4",
        passed=passed,
        sample_spec="console api test sample spec",
        population_size=100,
        sample_size=10,
        tolerance="bitwise",
        runtime_ms=5,
        evidence_digest="deadbeef",
        detail="ok",
    )


def _seed_artifact(pool, token: str, *, stage: str = "eval") -> str:
    """Insert an environments row and one artifact with typed inputs,
    returning the artifact_id — mirrors tests/integration/test_graph.py's
    real insert path (artifacts.env_fingerprint REFERENCES environments)."""

    env_fingerprint = f"env-console-{token}"

    def _environment(cur) -> None:
        cur.execute(
            """
            INSERT INTO environments
              (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            """,
            (env_fingerprint, "sha256:test", "3.12", Jsonb({"test": True}), 1),
        )

    in_txn(pool, _environment, op="test.console_api_environment")

    artifact_id = f"artifact-console-{token}"
    artifact = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage=stage,
        work_key=f"wk-console-{token}",
        s3_uri=f"s3://test/console/{token}",
        size_bytes=10,
        env_fingerprint=env_fingerprint,
        produced_by_run=uuid.uuid4(),
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    inputs = [
        ArtifactInput("env", "environment", env_fingerprint),
        ArtifactInput("config", "eval.metrics", "digest"),
    ]
    record_artifact(pool, artifact, inputs)
    return artifact_id


def test_health(pool) -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pipeline_reflects_latest_decision_and_artifact_per_stage(pool) -> None:
    token = uuid.uuid4().hex
    artifact_id = _seed_artifact(pool, token, stage="checkpoint")
    decision_id = record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-console-{token}",
            stage="checkpoint",
            action="RECOMPUTE",
            verdict="recompute",
            proposed_by="rule",
            latency_ms=7,
            explanation="console api pipeline test",
            candidate_artifact_id=artifact_id,
        ),
    )

    with TestClient(app) as client:
        response = client.get("/api/pipeline")
    assert response.status_code == 200
    body = response.json()

    stages = {entry["stage"]: entry for entry in body}
    assert set(stages) == {"env", "dataset", "features", "checkpoint", "eval"}

    checkpoint = stages["checkpoint"]
    # Validation fixtures remain durable and queryable by exact ID, but the
    # public pipeline view must not promote them over product runs.
    if checkpoint["latest_decision"] is not None:
        assert checkpoint["latest_decision"]["decision_id"] != str(decision_id)
    if checkpoint["latest_artifact"] is not None:
        assert checkpoint["latest_artifact"]["artifact_id"] != artifact_id


def test_decisions_list_is_paginated_and_ordered(pool) -> None:
    token = uuid.uuid4().hex
    decision_ids = []
    for i in range(3):
        decision_id = record_decision(
            pool,
            ReuseDecision(
                work_key=f"wk-console-page-{token}-{i}",
                stage="features",
                action="RECOMPUTE",
                verdict="recompute",
                proposed_by="rule",
                latency_ms=i,
                explanation=f"console api pagination test {i}",
            ),
        )
        decision_ids.append(str(decision_id))

    with TestClient(app) as client:
        response = client.get(
            "/api/decisions",
            params={"limit": 2, "offset": 0, "include_validation": True},
        )
        assert response.status_code == 200
        first_page = response.json()
        assert first_page["limit"] == 2
        assert first_page["offset"] == 0
        assert len(first_page["decisions"]) == 2
        assert first_page["total"] >= 3

        response = client.get(
            "/api/decisions",
            params={"limit": 2, "offset": 2, "include_validation": True},
        )
        assert response.status_code == 200
        second_page = response.json()
        assert len(second_page["decisions"]) >= 1

    first_ids = {row["decision_id"] for row in first_page["decisions"]}
    second_ids = {row["decision_id"] for row in second_page["decisions"]}
    assert first_ids.isdisjoint(second_ids)

    all_created_at = [row["created_at"] for row in first_page["decisions"]]
    assert all_created_at == sorted(all_created_at, reverse=True)


def test_decisions_list_rejects_invalid_limit_and_offset(pool) -> None:
    with TestClient(app) as client:
        response = client.get("/api/decisions", params={"limit": 0})
        assert response.status_code == 400

        response = client.get("/api/decisions", params={"limit": 201})
        assert response.status_code == 400

        response = client.get("/api/decisions", params={"offset": -1})
        assert response.status_code == 400


def test_decision_detail_includes_probe_and_artifact_inputs(pool) -> None:
    token = uuid.uuid4().hex
    artifact_id = _seed_artifact(pool, token, stage="eval")
    probe = _probe_result(passed=True)
    decision_id, probe_run_id = record_decision_with_probe(
        pool,
        ReuseDecision(
            work_key=f"wk-console-detail-{token}",
            stage="eval",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            authorized_by="probe",
            latency_ms=42,
            explanation="console api detail test",
            change_class="logging_only",
            candidate_artifact_id=artifact_id,
        ),
        probe,
        artifact_id=artifact_id,
    )

    with TestClient(app) as client:
        response = client.get(f"/api/decisions/{decision_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["decision"]["decision_id"] == str(decision_id)
    assert body["decision"]["work_key"] == f"wk-console-detail-{token}"
    assert body["decision"]["verdict"] == "reuse"
    assert body["decision"]["authorized_by"] == "probe"

    assert body["probe"] is not None
    assert body["probe"]["probe_run_id"] == str(probe_run_id)
    assert body["probe"]["probe_type"] == "P4"
    assert body["probe"]["passed"] is True

    input_pairs = {(edge["input_kind"], edge["input_ref"]) for edge in body["artifact_inputs"]}
    assert ("env", "environment") in input_pairs
    assert ("config", "eval.metrics") in input_pairs


def test_decision_detail_without_probe_has_null_probe_and_empty_inputs(pool) -> None:
    token = uuid.uuid4().hex
    decision_id = record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-console-noprobe-{token}",
            stage="dataset",
            action="RECOMPUTE",
            verdict="recompute",
            proposed_by="rule",
            latency_ms=3,
            explanation="console api no-probe test",
        ),
    )

    with TestClient(app) as client:
        response = client.get(f"/api/decisions/{decision_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["probe"] is None
    assert body["artifact_inputs"] == []


def test_decision_detail_404_for_unknown_id(pool) -> None:
    with TestClient(app) as client:
        response = client.get(f"/api/decisions/{uuid.uuid4()}")
    assert response.status_code == 404


def test_decision_detail_422_for_malformed_id(pool) -> None:
    with TestClient(app) as client:
        response = client.get("/api/decisions/not-a-uuid")
    assert response.status_code == 422
