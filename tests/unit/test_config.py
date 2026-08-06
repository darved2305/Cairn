from __future__ import annotations

import pytest

from cairn.config import ConfigError, TrackedConfig


def test_stage_reads_are_fully_qualified_and_isolated() -> None:
    config = TrackedConfig({"features": {"batch_size": 32}, "eval": {"metrics": ["accuracy"]}})

    assert config.stage("features").get("batch_size", expected_type=int) == 32
    assert config.stage("eval").get("metrics", expected_type=list) == ["accuracy"]

    assert config.reads("features") == {"features.batch_size": 32}
    assert config.reads("eval") == {"eval.metrics": ["accuracy"]}


def test_record_as_supports_stage_name_different_from_config_section() -> None:
    config = TrackedConfig({"train": {"hidden_dim": 256}})
    checkpoint = config.stage("train", record_as="checkpoint")
    assert checkpoint.get("hidden_dim") == 256
    assert config.reads("checkpoint") == {"train.hidden_dim": 256}
    assert config.reads("train") == {}


def test_default_is_recorded_because_it_was_causally_used() -> None:
    config = TrackedConfig({"eval": {}})
    assert config.stage("eval").get("metrics", default=["accuracy"]) == ["accuracy"]
    assert config.reads("eval") == {"eval.metrics": ["accuracy"]}


def test_returned_mutable_value_cannot_mutate_config_or_read_snapshot() -> None:
    config = TrackedConfig({"eval": {"metrics": ["accuracy"]}})
    metrics = config.stage("eval").get("metrics")
    metrics.append("macro_f1")
    assert config.reads("eval") == {"eval.metrics": ["accuracy"]}
    assert config.stage("eval").get("metrics") == ["accuracy"]


@pytest.mark.parametrize("path", ["", ".eval", "eval.", "eval..metrics"])
def test_invalid_dotted_paths_are_rejected(path: str) -> None:
    config = TrackedConfig({"eval": {}})
    with pytest.raises(ConfigError, match="invalid"):
        config.get(path, stage="eval")


def test_missing_required_key_and_wrong_type_are_clear_errors() -> None:
    config = TrackedConfig({"features": {"batch_size": "large"}})
    with pytest.raises(ConfigError, match="required config key"):
        config.stage("features").get("model")
    with pytest.raises(ConfigError, match="must be int"):
        config.stage("features").get("batch_size", expected_type=int)


def test_dotted_source_keys_are_rejected_to_avoid_ambiguous_paths() -> None:
    with pytest.raises(ConfigError, match="cannot contain"):
        TrackedConfig({"eval.metrics": ["accuracy"]})
