"""dataset stage: normalize whitespace, stable-sort by doc_id, produce the
deterministic train/test split. PROJECT.md §5.2: ~9s. Reads the vendored
raw snapshot (already header/footer/quote-stripped at vendor time — see
scripts/vendor_dataset.py) so this stage's own determinism never depends
on an external site being reachable at run time (PROJECT.md §5.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd
import pyarrow as pa

_WHITESPACE_RE = re.compile(r"\s+")

# Every Nth document (by doc_id, after the stable sort) is held out for
# eval. A fixed modulus instead of a seeded shuffle: zero dependency on
# any RNG implementation detail across Python/numpy versions, so the split
# itself can never be the thing that breaks a bit-identical rerun.
TEST_EVERY_NTH = 5


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


@dataclass(frozen=True)
class DatasetArtifact:
    parquet_bytes: bytes
    num_rows: int
    num_train: int
    num_test: int


def run(raw_snapshot_bytes: bytes) -> DatasetArtifact:
    df = pd.read_parquet(pa.BufferReader(raw_snapshot_bytes))
    df = df.sort_values("doc_id", kind="stable").reset_index(drop=True)

    df["text"] = df["text"].map(_normalize_whitespace)
    df = df[df["text"].str.len() > 0].reset_index(drop=True)
    df["split"] = ["test" if doc_id % TEST_EVERY_NTH == 0 else "train" for doc_id in df["doc_id"]]

    buf = df.to_parquet(index=False)
    num_test = int((df["split"] == "test").sum())
    return DatasetArtifact(
        parquet_bytes=buf,
        num_rows=len(df),
        num_train=len(df) - num_test,
        num_test=num_test,
    )
