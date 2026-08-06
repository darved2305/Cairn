from __future__ import annotations

import copy
from pathlib import Path

import yaml

from cairn.config import TrackedConfig
from cairn.planner import artifact_inputs, plan_pipeline

ROOT = Path(__file__).resolve().parents[2]


def _data() -> dict:
    loaded = yaml.safe_load((ROOT / "cairn.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _plan(data: dict):
    return plan_pipeline(
        TrackedConfig(data),
        source_root=ROOT / "src",
        env_fingerprint="test-env",
        data_fingerprint="test-data",
    ).by_stage()


def test_eval_metrics_change_only_eval_work_key() -> None:
    base = _data()
    changed = copy.deepcopy(base)
    changed["eval"]["metrics"] = ["accuracy"]

    before = _plan(base)
    after = _plan(changed)
    unchanged = {"env", "dataset", "features", "checkpoint"}
    assert all(before[stage].work_key.value == after[stage].work_key.value for stage in unchanged)
    assert before["eval"].work_key.value != after["eval"].work_key.value


def test_hidden_dim_change_keeps_features_but_invalidates_checkpoint_and_eval() -> None:
    base = _data()
    changed = copy.deepcopy(base)
    changed["train"]["hidden_dim"] = 512

    before = _plan(base)
    after = _plan(changed)
    assert before["features"].work_key.value == after["features"].work_key.value
    assert before["checkpoint"].work_key.value != after["checkpoint"].work_key.value
    assert before["eval"].work_key.value != after["eval"].work_key.value


def test_dataset_content_change_does_not_invalidate_environment_capture() -> None:
    config = TrackedConfig(_data())
    before = plan_pipeline(
        config,
        source_root=ROOT / "src",
        env_fingerprint="test-env",
        data_fingerprint="dataset-a",
    ).by_stage()
    after = plan_pipeline(
        TrackedConfig(_data()),
        source_root=ROOT / "src",
        env_fingerprint="test-env",
        data_fingerprint="dataset-b",
    ).by_stage()
    assert before["env"].work_key.value == after["env"].work_key.value
    assert before["dataset"].work_key.value != after["dataset"].work_key.value
    assert before["features"].work_key.value != after["features"].work_key.value


def test_artifact_inputs_cover_every_work_key_component_without_duplicate_identity() -> None:
    stage = _plan(_data())["checkpoint"]
    inputs = artifact_inputs(stage)
    identities = {(item.input_kind, item.input_ref) for item in inputs}
    assert len(inputs) == len(identities)
    assert {item.input_kind for item in inputs} == {"code", "config", "data", "upstream", "env"}
