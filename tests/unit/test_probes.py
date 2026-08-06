from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import torch

from cairn.fingerprint.reach import build_graph
from cairn.probes import p1_env, p2_struct, p4_logits, p5_schema, p6_evalslice
from cairn.probes.base import hash_select
from cairn.workload.stage_train import Classifier

# ---------------------------------------------------------------------------
# base.hash_select
# ---------------------------------------------------------------------------


def test_hash_select_returns_exactly_k_regardless_of_population_size() -> None:
    population = list(range(1000))
    selected = hash_select("salt", population, 64)
    assert len(selected) == 64
    assert len(set(selected)) == 64


def test_hash_select_is_deterministic_given_the_same_salt() -> None:
    population = list(range(50))
    assert hash_select("artifact-a", population, 10) == hash_select("artifact-a", population, 10)


def test_hash_select_differs_across_salts() -> None:
    population = list(range(50))
    assert hash_select("artifact-a", population, 10) != hash_select("artifact-b", population, 10)


# ---------------------------------------------------------------------------
# P1 env_identity
# ---------------------------------------------------------------------------


def test_p1_passes_on_identical_fingerprints() -> None:
    result = p1_env.run("env-abc", "env-abc")
    assert result.passed
    assert result.probe_type == "P1"
    assert result.population_size == 1 and result.sample_size == 1


def test_p1_fails_on_differing_fingerprints() -> None:
    result = p1_env.run("env-abc", "env-xyz")
    assert not result.passed
    assert "differs" in result.detail


# ---------------------------------------------------------------------------
# P2 structural_unreachable
# ---------------------------------------------------------------------------


def _pkg(tmp_path: Path, body: str) -> Path:
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "stage.py").write_text(body, encoding="utf-8")
    return tmp_path


def test_p2_passes_when_changed_unit_is_unreachable(tmp_path: Path) -> None:
    root = _pkg(tmp_path, "def run():\n    return 1\n\ndef dead():\n    return 2\n")
    graph = build_graph(root)
    result = p2_struct.run(graph, graph, "demo.stage:run", {"demo.stage:dead"})
    assert result.passed


def test_p2_fails_when_changed_unit_is_reachable(tmp_path: Path) -> None:
    root = _pkg(tmp_path, "def run():\n    return helper()\n\ndef helper():\n    return 2\n")
    graph = build_graph(root)
    result = p2_struct.run(graph, graph, "demo.stage:run", {"demo.stage:helper"})
    assert not result.passed


def test_p2_fails_when_reachability_is_unsound(tmp_path: Path) -> None:
    root = _pkg(
        tmp_path,
        "def run():\n    return getattr(object(), 'x', None)\n\ndef dead():\n    return 1\n",
    )
    graph = build_graph(root)
    result = p2_struct.run(graph, graph, "demo.stage:run", {"demo.stage:dead"})
    assert not result.passed
    assert "getattr" in result.detail


# ---------------------------------------------------------------------------
# P4 checkpoint_logit
# ---------------------------------------------------------------------------


def _random_state_dict_bytes(
    seed: int, *, input_dim: int, hidden_dim: int, num_labels: int
) -> bytes:
    from io import BytesIO

    torch.manual_seed(seed)
    model = Classifier(input_dim=input_dim, hidden_dim=hidden_dim, num_labels=num_labels)
    buf = BytesIO()
    torch.save(model.state_dict(), buf)
    return buf.getvalue()


def test_p4_passes_when_checkpoint_bytes_are_identical() -> None:
    state = _random_state_dict_bytes(1, input_dim=8, hidden_dim=4, num_labels=3)
    doc_ids = list(range(20))
    embeddings = np.random.default_rng(0).normal(size=(20, 8)).astype(np.float32)

    result = p4_logits.run(
        "artifact-1",
        doc_ids,
        embeddings,
        state,
        state,
        input_dim=8,
        hidden_dim=4,
        num_labels=3,
        sample_size=10,
    )
    assert result.passed
    assert result.sample_size == 10
    assert result.population_size == 20


def test_p4_fails_when_checkpoints_differ() -> None:
    old_state = _random_state_dict_bytes(1, input_dim=8, hidden_dim=4, num_labels=3)
    new_state = _random_state_dict_bytes(2, input_dim=8, hidden_dim=4, num_labels=3)
    doc_ids = list(range(20))
    embeddings = np.random.default_rng(0).normal(size=(20, 8)).astype(np.float32)

    result = p4_logits.run(
        "artifact-1",
        doc_ids,
        embeddings,
        old_state,
        new_state,
        input_dim=8,
        hidden_dim=4,
        num_labels=3,
        sample_size=10,
    )
    assert not result.passed


# ---------------------------------------------------------------------------
# P5 schema_stats
# ---------------------------------------------------------------------------


def _parquet_bytes(n: int, offset: int = 0) -> bytes:
    df = pd.DataFrame({"doc_id": range(n), "value": [float(i + offset) for i in range(n)]})
    buf: bytes = df.to_parquet(index=False)
    return buf


def test_p5_passes_on_identical_tables() -> None:
    data = _parquet_bytes(100)
    result = p5_schema.run(data, data)
    assert result.passed
    assert result.population_size == 100
    assert result.sample_size == 10


def test_p5_fails_on_schema_mismatch() -> None:
    old = _parquet_bytes(100)
    df = pd.DataFrame({"doc_id": range(100), "other_name": [float(i) for i in range(100)]})
    new: bytes = df.to_parquet(index=False)
    result = p5_schema.run(old, new)
    assert not result.passed
    assert "schema differs" in result.detail


def test_p5_fails_when_slice_content_differs() -> None:
    old = _parquet_bytes(100)
    new = _parquet_bytes(100, offset=1)
    result = p5_schema.run(old, new)
    assert not result.passed
    assert "checksum" in result.detail


def test_p5_handles_empty_tables() -> None:
    data = _parquet_bytes(0)
    result = p5_schema.run(data, data)
    assert result.passed
    assert result.population_size == 0
    assert result.sample_size == 0


# ---------------------------------------------------------------------------
# P6 eval_slice_replay
# ---------------------------------------------------------------------------


def test_p6_passes_when_checkpoint_bytes_are_identical() -> None:
    state = _random_state_dict_bytes(1, input_dim=8, hidden_dim=4, num_labels=3)
    doc_ids = list(range(300))
    rng = np.random.default_rng(0)
    embeddings = rng.normal(size=(300, 8)).astype(np.float32)
    labels = rng.integers(0, 3, size=300).astype(np.int64)

    result = p6_evalslice.run(
        "artifact-1",
        doc_ids,
        embeddings,
        labels,
        state,
        state,
        metrics=["accuracy", "macro_f1"],
        input_dim=8,
        hidden_dim=4,
        num_labels=3,
    )
    assert result.passed
    assert result.sample_size == 200


def test_p6_fails_when_checkpoints_differ() -> None:
    old_state = _random_state_dict_bytes(1, input_dim=8, hidden_dim=4, num_labels=3)
    new_state = _random_state_dict_bytes(2, input_dim=8, hidden_dim=4, num_labels=3)
    doc_ids = list(range(300))
    rng = np.random.default_rng(0)
    embeddings = rng.normal(size=(300, 8)).astype(np.float32)
    labels = rng.integers(0, 3, size=300).astype(np.int64)

    result = p6_evalslice.run(
        "artifact-1",
        doc_ids,
        embeddings,
        labels,
        old_state,
        new_state,
        metrics=["accuracy"],
        input_dim=8,
        hidden_dim=4,
        num_labels=3,
    )
    assert not result.passed
