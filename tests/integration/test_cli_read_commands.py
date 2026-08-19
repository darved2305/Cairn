"""Integration tests for the Phase 0 read-only CLI commands — `init`
(already unit-tested against tmp_path, no DB needed), `explain`, `memory
search`/`memory why-blocked`, and `doctor`.

Every test seeds real rows through the actual db-layer write functions
against the live local CockroachDB cluster (docs/project/PLAN.md §5's anti-simulation
rule — no mocked DB). `explain`/`memory`/`doctor` all call `close_pool()`
on exit (same as every other Cairn CLI command), so — mirroring
tests/integration/test_cli_claim_demo.py's own docstring — each test seeds
through a freshly-fetched `get_pool()` rather than holding onto the `pool`
fixture's value directly; `pool` is kept only for its skip-if-no-
CAIRN_DATABASE_URL behavior.
"""

from __future__ import annotations

import json
import uuid

import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from cairn.cli import app
from cairn.db import claims
from cairn.db.contradictions import record_contradiction
from cairn.db.decisions import ReuseDecision, record_decision
from cairn.db.graph import ArtifactInput, record_artifact
from cairn.db.memory import ChangedKey, FailureSignature, Remediation
from cairn.db.memory import record_failure_signature as _record_failure_signature
from cairn.db.memory import record_remediation as _record_remediation
from cairn.db.pool import get_pool
from cairn.db.txn import in_txn
from cairn.embeddings import default_provider

pytestmark = pytest.mark.integration


def _seed_environment(pool, env_fingerprint: str) -> None:
    def _tx(cur):  # noqa: ANN001, ANN202 - local test helper
        cur.execute(
            """
            INSERT INTO environments
              (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (env_fingerprint) DO NOTHING
            """,
            (env_fingerprint, "sha256:test", "3.12", Jsonb({"test": True}), 1),
        )

    in_txn(pool, _tx, op="test.read_commands_environment")


def _seed_artifact(pool, token: str, *, stage: str = "eval", inputs=None) -> str:
    env_fingerprint = f"env-readcmd-{token}"
    _seed_environment(pool, env_fingerprint)
    artifact_id = f"artifact-readcmd-{token}"
    artifact = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage=stage,
        work_key=f"wk-readcmd-{token}",
        s3_uri=f"s3://test/readcmd/{token}",
        size_bytes=10,
        env_fingerprint=env_fingerprint,
        produced_by_run=uuid.uuid4(),
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    record_artifact(pool, artifact, inputs or [])
    return artifact_id


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def test_explain_json_reports_full_provenance(pool) -> None:
    token = uuid.uuid4().hex
    get_pool()  # see module docstring
    upstream_id = _seed_artifact(pool, f"{token}-up", stage="features")
    artifact_id = _seed_artifact(
        pool,
        token,
        stage="checkpoint",
        inputs=[ArtifactInput("upstream", upstream_id, upstream_id)],
    )
    decision_id = record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-readcmd-{token}",
            stage="checkpoint",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            authorized_by="probe",
            latency_ms=12,
            explanation="explain-command integration test",
            candidate_artifact_id=artifact_id,
        ),
    )
    contradiction = record_contradiction(
        pool, artifact_id, uuid.uuid4(), evidence="explain-command integration test evidence"
    )

    result = CliRunner().invoke(app, ["explain", artifact_id, "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["artifact_id"] == artifact_id
    assert payload["stage"] == "checkpoint"
    assert {i["input_ref"] for i in payload["inputs"]} == {upstream_id}
    assert any(d["decision_id"] == str(decision_id) for d in payload["decisions"])
    assert any(
        c["contradiction_id"] == str(contradiction.contradiction_id)
        for c in payload["contradictions"]
    )

    # the upstream artifact's explain output must list `artifact_id` as downstream
    upstream_result = CliRunner().invoke(app, ["explain", upstream_id, "--output", "json"])
    assert upstream_result.exit_code == 0, upstream_result.output
    upstream_payload = json.loads(upstream_result.stdout)
    assert artifact_id in upstream_payload["downstream"]


def test_explain_exits_1_for_unknown_artifact(pool) -> None:
    get_pool()
    result = CliRunner().invoke(app, ["explain", f"does-not-exist-{uuid.uuid4().hex}"])
    assert result.exit_code == 1
    assert "unknown artifact_id" in result.output


def test_explain_table_output_is_human_readable(pool) -> None:
    token = uuid.uuid4().hex
    get_pool()
    artifact_id = _seed_artifact(pool, token, stage="eval")

    result = CliRunner().invoke(app, ["explain", artifact_id])
    assert result.exit_code == 0, result.output
    assert artifact_id in result.output
    assert "downstream" in result.output
    assert "decisions" in result.output
    assert "contradictions" in result.output


# ---------------------------------------------------------------------------
# memory search / why-blocked
# ---------------------------------------------------------------------------


def test_memory_search_finds_a_seeded_failure_offline(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    get_pool()
    token = uuid.uuid4().hex
    summary_text = f"cli memory search integration test failure {token}"
    _record_failure_signature(
        pool,
        FailureSignature(
            stage="checkpoint",
            error_class="ValueError",
            workload_kind="text-classification",
            traceback_head=f"ValueError: {token}",
            summary_text=summary_text,
            embedding=default_provider().embed(summary_text),
            run_id=uuid.uuid4(),
            wasted_ms=500,
        ),
    )

    result = CliRunner().invoke(app, ["memory", "search", summary_text, "--stage", "checkpoint"])
    assert result.exit_code == 0, result.output
    assert "provider=" in result.output
    assert token in result.output


def test_memory_search_reports_no_matches_for_an_unrelated_stage(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    get_pool()
    result = CliRunner().invoke(
        app,
        [
            "memory",
            "search",
            f"nothing should ever match this exact string {uuid.uuid4().hex}",
            "--stage",
            f"nonexistent-stage-{uuid.uuid4().hex}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no matches" in result.output


def test_memory_why_blocked_reports_the_latest_refusal(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    get_pool()
    token = uuid.uuid4().hex
    record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-why-blocked-{token}",
            stage="checkpoint",
            action="REFUSE_DOOMED",
            verdict="refused",
            proposed_by="rule",
            latency_ms=0,
            explanation=f"why-blocked integration test refusal {token}",
        ),
    )

    result = CliRunner().invoke(app, ["memory", "why-blocked"])
    assert result.exit_code == 0, result.output
    assert token in result.output


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_passes_database_and_schema_checks_against_the_live_cluster(pool) -> None:
    get_pool()
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "PASS  database" in result.output
    assert "PASS  schema" in result.output


def test_memory_remediation_join_is_visible_through_search(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a tiering test (memory search never tiers — see cli.py's
    docstring) — just proves the CLI's search_text() call surfaces a
    signature that has a real succeeded remediation on record, same LATERAL
    join db/memory.py::search uses."""

    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    get_pool()
    token = uuid.uuid4().hex
    summary_text = f"cli memory remediation join test {token}"
    signature_id = _record_failure_signature(
        pool,
        FailureSignature(
            stage="checkpoint",
            error_class="RuntimeError",
            workload_kind="text-classification",
            traceback_head=f"RuntimeError: {token}",
            summary_text=summary_text,
            embedding=default_provider().embed(summary_text),
            run_id=uuid.uuid4(),
            wasted_ms=500,
        ),
    )
    _record_remediation(
        pool,
        Remediation(
            signature_id=signature_id,
            changed_keys=[ChangedKey(key="train.input_dim", from_value=384, to_value=768)],
            rationale="cli read-command test remediation",
            verified_run_id=uuid.uuid4(),
            succeeded=True,
        ),
    )

    result = CliRunner().invoke(app, ["memory", "search", summary_text])
    assert result.exit_code == 0, result.output
    assert token in result.output
