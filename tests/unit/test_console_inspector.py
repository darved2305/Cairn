from __future__ import annotations

import subprocess

import pytest

from cairn.console import inspector


def test_windows_inspector_native_failure_becomes_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["python"],
            3221226505,
            stdout="",
            stderr="OPENSSL_Uplink: no OPENSSL_Applink",
        )

    monkeypatch.setattr(inspector.subprocess, "run", fake_run)

    with pytest.raises(inspector.InspectorUnavailable, match="OPENSSL_Uplink"):
        inspector._ask_windows_subprocess("Which failures were refused?")


def test_windows_inspector_rehydrates_a_real_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        '{"ok":true,"answer":{"answer":"Two failures.",'
        '"executed_sql":"SELECT count(*) FROM failure_signatures LIMIT 25",'
        '"tool_backend":"direct_sql","tool_calls":[],"model_id":"model",'
        '"rounds":2,"truncated":false}}'
    )

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["python"], 0, stdout=payload, stderr="")

    monkeypatch.setattr(inspector.subprocess, "run", fake_run)

    result = inspector._ask_windows_subprocess("How many failures?")

    assert result.answer == "Two failures."
    assert result.executed_sql.startswith("SELECT count(*)")
    assert result.rounds == 2
