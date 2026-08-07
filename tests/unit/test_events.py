from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.obs.events import PROTOCOL_VERSION, close_events_stream, emit_event


@pytest.fixture(autouse=True)
def _reset_events_stream() -> None:
    close_events_stream()
    yield
    close_events_stream()


def test_emit_event_is_a_silent_no_op_without_cairn_events_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CAIRN_EVENTS_FILE", raising=False)
    emit_event("run.started", {"target_stage": "eval"})
    # Nothing to assert against a file that was never created — the point
    # is this must not raise, and must not create any file as a side effect.
    assert list(tmp_path.iterdir()) == []


def test_emit_event_writes_a_versioned_ndjson_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_file = tmp_path / "events.ndjson"
    monkeypatch.setenv("CAIRN_EVENTS_FILE", str(events_file))

    emit_event("claim.acquired", {"work_key": "wk-1", "fence": 1}, run_id="run-abc")

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["version"] == PROTOCOL_VERSION
    assert envelope["type"] == "claim.acquired"
    assert envelope["run_id"] == "run-abc"
    assert envelope["payload"] == {"work_key": "wk-1", "fence": 1}
    assert isinstance(envelope["timestamp"], str) and envelope["timestamp"]


def test_emit_event_appends_multiple_events_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events_file = tmp_path / "events.ndjson"
    monkeypatch.setenv("CAIRN_EVENTS_FILE", str(events_file))

    emit_event("run.started", {"target_stage": "eval"})
    emit_event("stage.started", {"stage": "env"})
    emit_event("stage.completed", {"stage": "env", "verdict": "reuse"})

    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["run.started", "stage.started", "stage.completed"]


def test_emit_event_serializes_non_json_native_values_via_str(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uuid

    events_file = tmp_path / "events.ndjson"
    monkeypatch.setenv("CAIRN_EVENTS_FILE", str(events_file))

    run_id = uuid.uuid4()
    emit_event("run.completed", {"produced_by_run": run_id})

    envelope = json.loads(events_file.read_text(encoding="utf-8").strip())
    assert envelope["payload"]["produced_by_run"] == str(run_id)
