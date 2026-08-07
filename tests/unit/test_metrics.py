from __future__ import annotations

import json

import pytest

from cairn.obs.metrics import NAMESPACE, emit_metric


def test_emit_metric_writes_valid_emf_json(capsys: pytest.CaptureFixture[str]) -> None:
    emit_metric("TxnRetries40001", 3, dimensions={"op": "claim.acquire"})
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)

    assert payload["TxnRetries40001"] == 3
    assert payload["op"] == "claim.acquire"
    metrics_block = payload["_aws"]["CloudWatchMetrics"][0]
    assert metrics_block["Namespace"] == NAMESPACE
    assert metrics_block["Metrics"] == [{"Name": "TxnRetries40001", "Unit": "Count"}]
    assert metrics_block["Dimensions"] == [["op"]]


def test_emit_metric_without_dimensions_uses_empty_dimension_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    emit_metric("QuarantineEvents", 1)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]
