"""Proves the Python -> event-file bridge end-to-end against the real
claim protocol and decision ledger — not just obs/events.py's own unit
tests (which never touch CockroachDB), but the actual call sites wired
into db/claims.py and db/decisions.py. docs/project/PLAN.md §5's anti-simulation rule
applies here too: every payload asserted below was written by real SQL
against the live local cluster, not fabricated by the test.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from cairn.db import claims
from cairn.db.decisions import ReuseDecision, record_decision
from cairn.db.environments import ensure_environment
from cairn.obs.events import close_events_stream
from cairn.workload import stage_env

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_events_stream():  # noqa: ANN201
    close_events_stream()
    yield
    close_events_stream()


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line]


def test_claim_acquire_and_complete_emit_real_events(
    pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_file = tmp_path / "events.ndjson"
    monkeypatch.setenv("CAIRN_EVENTS_FILE", str(events_file))

    token = uuid.uuid4().hex
    work_key = f"events-test-{token}"
    run_id = uuid.uuid4()
    environment = stage_env.run()
    ensure_environment(
        pool,
        env_fingerprint=environment.env_fingerprint,
        image_digest=environment.image_digest,
        python_version=environment.python_version,
        deps=environment.deps,
        torch_threads=environment.torch_threads,
    )

    claim = claims.acquire(pool, work_key, "eval", f"owner-{token}", "host-a", "us-east-1", run_id)
    assert claim.won

    artifact_id = f"artifact-{token}"
    record = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage="eval",
        work_key=work_key,
        s3_uri=f"s3://cairn-test/{artifact_id}",
        size_bytes=42,
        env_fingerprint=environment.env_fingerprint,
        produced_by_run=run_id,
        duration_ms=17,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    assert claims.complete(pool, work_key, f"owner-{token}", claim.fence, record)

    events = _read_events(events_file)
    types = [e["type"] for e in events]
    assert "claim.acquired" in types
    assert "claim.completed" in types

    acquired = next(e for e in events if e["type"] == "claim.acquired")
    assert acquired["payload"]["work_key"] == work_key
    assert acquired["payload"]["fence"] == claim.fence
    assert acquired["run_id"] == str(run_id)

    completed = next(e for e in events if e["type"] == "claim.completed")
    assert completed["payload"]["artifact_id"] == artifact_id
    assert completed["payload"]["duration_ms"] == 17


def test_record_decision_emits_a_real_decision_event(pool, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    events_file = tmp_path / "events.ndjson"
    monkeypatch.setenv("CAIRN_EVENTS_FILE", str(events_file))
    token = uuid.uuid4().hex

    decision_id = record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-events-{token}",
            stage="checkpoint",
            action="RECOMPUTE",
            verdict="recompute",
            proposed_by="rule",
            latency_ms=9,
            explanation="events integration test",
        ),
    )

    events = _read_events(events_file)
    decision_events = [e for e in events if e["type"] == "decision.recorded"]
    assert len(decision_events) == 1
    assert decision_events[0]["payload"]["decision_id"] == str(decision_id)
    assert decision_events[0]["payload"]["action"] == "RECOMPUTE"
    assert decision_events[0]["payload"]["verdict"] == "recompute"


def test_events_file_unset_produces_no_event_side_effects(pool, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("CAIRN_EVENTS_FILE", raising=False)
    token = uuid.uuid4().hex
    # Must not raise, and — by construction of obs/events.py — writes nothing.
    record_decision(
        pool,
        ReuseDecision(
            work_key=f"wk-noevents-{token}",
            stage="eval",
            action="RECOMPUTE",
            verdict="recompute",
            proposed_by="rule",
            latency_ms=1,
            explanation="no events file set",
        ),
    )
