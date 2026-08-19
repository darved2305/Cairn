from __future__ import annotations

import uuid

import psycopg
import pytest

from cairn.db import decisions
from cairn.db.decisions import InvalidDecision, ReuseDecision
from cairn.db.txn import in_txn
from cairn.probes.base import ProbeResult

pytestmark = pytest.mark.integration


def _probe_result(*, passed: bool = True) -> ProbeResult:
    return ProbeResult(
        probe_type="P4",
        passed=passed,
        sample_spec="test sample spec",
        population_size=100,
        sample_size=10,
        tolerance="bitwise",
        runtime_ms=5,
        evidence_digest="deadbeef",
        detail="ok",
    )


def test_database_rejects_model_authorized_reuse_even_bypassing_python(pool) -> None:
    """The CHECK constraint, not this module's __post_init__, is the real
    guarantee — docs/project/PROJECT.md §3.1. Prove it holds even for a raw INSERT that
    never goes through ReuseDecision at all."""

    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO reuse_decisions
              (work_key, stage, action, verdict, proposed_by, authorized_by, latency_ms, explanation)
            VALUES (%s,'eval','REUSE','reuse','model','model',5,'bypassing python entirely')
            """,
            (f"wk-{uuid.uuid4().hex}",),
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        in_txn(pool, _tx, op="test.raw_model_authorized_reuse")


def test_database_rejects_reuse_with_null_authorized_by(pool) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO reuse_decisions
              (work_key, stage, action, verdict, proposed_by, latency_ms, explanation)
            VALUES (%s,'eval','REUSE','reuse','rule',5,'no authorized_by at all')
            """,
            (f"wk-{uuid.uuid4().hex}",),
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        in_txn(pool, _tx, op="test.raw_null_authorized_by")


def test_python_guard_rejects_authorized_by_model_as_an_invalid_value() -> None:
    # 'model' isn't a member of AUTHORIZED_BY at all — rejected regardless
    # of verdict, which is a stronger guarantee than only checking on reuse.
    with pytest.raises(InvalidDecision, match="authorized_by must be one of"):
        ReuseDecision(
            work_key="wk-1",
            stage="eval",
            action="REUSE",
            verdict="reuse",
            proposed_by="model",
            authorized_by="model",  # type: ignore[arg-type]
            latency_ms=1,
            explanation="should never construct",
        )


def test_python_guard_rejects_reuse_verdict_with_no_authorized_by() -> None:
    with pytest.raises(InvalidDecision, match="deterministic evidence must authorize"):
        ReuseDecision(
            work_key="wk-1b",
            stage="eval",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            latency_ms=1,
            explanation="should never construct — no authorized_by given",
        )


def test_recompute_verdict_does_not_require_authorized_by() -> None:
    decision = ReuseDecision(
        work_key="wk-2",
        stage="checkpoint",
        action="RECOMPUTE",
        verdict="recompute",
        proposed_by="rule",
        latency_ms=1,
        explanation="hidden_dim changed",
    )
    assert decision.authorized_by is None


def test_record_decision_with_probe_writes_both_rows_atomically(pool) -> None:
    probe = _probe_result(passed=True)
    decision = ReuseDecision(
        work_key=f"wk-{uuid.uuid4().hex}",
        stage="checkpoint",
        action="REUSE",
        verdict="reuse",
        proposed_by="rule",
        authorized_by="probe",
        latency_ms=42,
        explanation="logging_only class + P4 passed",
        change_class="logging_only",
        candidate_artifact_id="artifact-xyz",
    )

    decision_id, probe_run_id = decisions.record_decision_with_probe(
        pool, decision, probe, artifact_id="artifact-xyz"
    )

    def _fetch(cur: psycopg.Cursor) -> tuple[str, str, bool]:
        cur.execute(
            "SELECT authorized_by, verdict, passed FROM reuse_decisions "
            "JOIN probe_runs ON reuse_decisions.probe_run_id = probe_runs.probe_run_id "
            "WHERE decision_id = %s",
            (decision_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return row

    authorized_by, verdict, passed = in_txn(pool, _fetch, op="test.fetch_decision_with_probe")
    assert authorized_by == "probe"
    assert verdict == "reuse"
    assert passed is True


def test_record_decision_with_probe_rejects_pre_set_probe_run_id(pool) -> None:
    probe = _probe_result()
    decision = ReuseDecision(
        work_key="wk-3",
        stage="eval",
        action="REUSE",
        verdict="reuse",
        proposed_by="rule",
        authorized_by="probe",
        probe_run_id=uuid.uuid4(),
        latency_ms=1,
        explanation="should be rejected before any write",
    )
    with pytest.raises(InvalidDecision, match="probe_run_id must be None"):
        decisions.record_decision_with_probe(pool, decision, probe)


def test_record_probe_run_and_record_decision_standalone(pool) -> None:
    probe_run_id = decisions.record_probe_run(pool, _probe_result(), artifact_id="artifact-abc")
    decision_id = decisions.record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-{uuid.uuid4().hex}",
            stage="features",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            authorized_by="structural",
            probe_run_id=probe_run_id,
            latency_ms=3,
            explanation="unreachable_change",
        ),
    )
    assert isinstance(decision_id, uuid.UUID)
