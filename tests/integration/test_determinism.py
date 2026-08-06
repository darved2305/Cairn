"""Determinism proof: PROJECT.md §5.2 — "same image + same inputs =>
bit-identical outputs, asserted over 3 runs." Runs the features and
checkpoint stages three times on a small synthetic corpus and asserts
every byte matches.

Small corpus, not the real ~3200-document dataset: the property under
test is determinism of the *computation*, not its runtime on full data —
PLAN.md's ~95s/~28s stage timings are what the real pipeline costs, not
what proving this property costs. Marked integration rather than unit
because it downloads and runs the real MiniLM model (network on first
call, then cached) — no mock stands in for it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cairn.workload import determinism, stage_features, stage_train

pytestmark = pytest.mark.integration

_TOPICS = [
    "The rocket launched into orbit carrying a new communications satellite payload.",
    "The mechanic replaced the timing belt and checked the brake pads carefully.",
    "The graphics card renders millions of triangles at a smooth frame rate.",
    "Diplomats met for hours to discuss the ceasefire terms and regional tensions.",
]


def _synthetic_dataset(n: int = 20) -> pd.DataFrame:
    rows = [{"doc_id": i, "text": _TOPICS[i % 4], "target": i % 4} for i in range(n)]
    df = pd.DataFrame(rows)
    df["split"] = ["test" if doc_id % 5 == 0 else "train" for doc_id in df["doc_id"]]
    return df


def _run_features_and_checkpoint() -> tuple[bytes, bytes]:
    determinism.apply()
    df = _synthetic_dataset()
    dataset_bytes = df.to_parquet(index=False)

    features = stage_features.run(dataset_bytes, shard_count=2)
    joined = stage_train.join_dataset_and_features(dataset_bytes, features.parquet_bytes)
    # 2 epochs is enough to exercise the full forward/backward/optimizer
    # step at least twice — determinism is what's under test, not
    # convergence, so there's no reason to pay for all 12 epochs here.
    checkpoint = stage_train.run(joined, epochs=2)

    return features.parquet_bytes, checkpoint.state_dict_bytes


def test_features_and_checkpoint_are_bit_identical_across_three_runs(
    warm_embedding_model: None,
) -> None:
    runs = [_run_features_and_checkpoint() for _ in range(3)]
    features_bytes = [r[0] for r in runs]
    checkpoint_bytes = [r[1] for r in runs]

    assert features_bytes[0] == features_bytes[1], "features run 1 vs 2 differ"
    assert features_bytes[1] == features_bytes[2], "features run 2 vs 3 differ"
    assert checkpoint_bytes[0] == checkpoint_bytes[1], "checkpoint run 1 vs 2 differ"
    assert checkpoint_bytes[1] == checkpoint_bytes[2], "checkpoint run 2 vs 3 differ"
