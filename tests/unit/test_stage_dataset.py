"""Unit tests for cairn.workload.stage_dataset — pure pandas/pyarrow logic,
no model, no network, no S3. The vendored-snapshot input is faked with a
small synthetic frame matching scripts/vendor_dataset.py's schema.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

from cairn.workload import stage_dataset


def _raw_bytes(rows: list[dict]) -> bytes:
    df = pd.DataFrame(rows)
    return df.to_parquet(index=False)


def _read(parquet_bytes: bytes) -> pd.DataFrame:
    return pd.read_parquet(pa.BufferReader(parquet_bytes))


def test_normalizes_internal_whitespace() -> None:
    raw = _raw_bytes(
        [{"doc_id": 0, "text": "hello\n\n  world\t!", "target": 0, "target_name": "a"}]
    )
    out = _read(stage_dataset.run(raw).parquet_bytes)
    assert out.loc[0, "text"] == "hello world !"


def test_drops_rows_that_become_empty_after_normalization() -> None:
    raw = _raw_bytes(
        [
            {"doc_id": 0, "text": "   ", "target": 0, "target_name": "a"},
            {"doc_id": 1, "text": "real content", "target": 1, "target_name": "b"},
        ]
    )
    artifact = stage_dataset.run(raw)
    out = _read(artifact.parquet_bytes)
    assert artifact.num_rows == 1
    assert list(out["doc_id"]) == [1]


def test_split_is_deterministic_every_nth_by_doc_id() -> None:
    rows = [
        {"doc_id": i, "text": f"doc {i}", "target": i % 4, "target_name": "x"} for i in range(20)
    ]
    artifact = stage_dataset.run(_raw_bytes(rows))
    out = _read(artifact.parquet_bytes)

    expected_test_ids = {i for i in range(20) if i % stage_dataset.TEST_EVERY_NTH == 0}
    actual_test_ids = set(out.loc[out["split"] == "test", "doc_id"])
    assert actual_test_ids == expected_test_ids
    assert artifact.num_test == len(expected_test_ids)
    assert artifact.num_train == 20 - len(expected_test_ids)


def test_stable_sort_by_doc_id_regardless_of_input_order() -> None:
    rows = [
        {"doc_id": 2, "text": "c", "target": 0, "target_name": "x"},
        {"doc_id": 0, "text": "a", "target": 0, "target_name": "x"},
        {"doc_id": 1, "text": "b", "target": 0, "target_name": "x"},
    ]
    out = _read(stage_dataset.run(_raw_bytes(rows)).parquet_bytes)
    assert list(out["doc_id"]) == [0, 1, 2]


def test_run_is_deterministic_across_calls() -> None:
    rows = [
        {"doc_id": i, "text": f"  doc {i}  ", "target": i % 4, "target_name": "x"}
        for i in range(15)
    ]
    raw = _raw_bytes(rows)
    first = stage_dataset.run(raw).parquet_bytes
    second = stage_dataset.run(raw).parquet_bytes
    assert first == second
