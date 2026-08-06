from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cairn.cli import app

ROOT = Path(__file__).resolve().parents[2]


def test_plan_json_prints_all_stage_work_keys() -> None:
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--config",
            str(ROOT / "cairn.yaml"),
            "--source-root",
            str(ROOT / "src"),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [stage["stage"] for stage in payload["stages"]] == [
        "env",
        "dataset",
        "features",
        "checkpoint",
        "eval",
    ]
    assert all(len(stage["work_key"]) == 64 for stage in payload["stages"])


def test_plan_rejects_invalid_output_mode() -> None:
    result = CliRunner().invoke(app, ["plan", "--output", "xml"])
    assert result.exit_code == 2
    assert "table" in result.output
