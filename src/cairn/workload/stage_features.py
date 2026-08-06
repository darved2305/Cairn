"""features stage: sentence-transformers/all-MiniLM-L6-v2 embeddings,
384-dim fp32, batch 32, max_seq_length=256. PROJECT.md §5.2: ~95s, the
expensive stage — and the one whose partial reuse across an unrelated
architecture change is the "money shot" PROJECT.md §4.3 is built around.

Embeddings are written with pyarrow directly (a FixedSizeListArray column)
rather than through a pandas object column of numpy arrays, so the on-disk
dtype and shape are exact and don't depend on pandas' parquet-conversion
inference for object columns.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from cairn.config import StageConfig
from cairn.fingerprint.canon import canonical_float32_bytes

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
MAX_SEQ_LENGTH = 256
BATCH_SIZE = 32
SHARD_COUNT = 3

_models: dict[str, Any] = {}


def _get_model(max_seq_length: int, model_name: str = MODEL_NAME) -> Any:
    """Loaded once per process, not per shard — the model load itself is
    fixed cost that sharding is not trying to save; sharding exists for
    fragmentable, resumable *compute*, not for amortizing model load.

    max_seq_length is re-applied on every call (cheap attribute set, no
    reload) rather than baked in at load time — PROJECT.md §5.3's F3
    scenario is exactly a caller passing an oversized max_seq_length, and
    that has to reach the live model, not a cached default."""
    if model_name not in _models:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError
        from sentence_transformers import SentenceTransformer

        configured_path = os.environ.get("CAIRN_MODEL_PATH")
        if configured_path:
            source = configured_path
        else:
            # Prefer an already-vendored/cached snapshot. Apart from making
            # cold starts faster, this avoids a needless network HEAD request
            # on every worker boot. A genuinely clean dev machine still falls
            # back to SentenceTransformer's normal one-time download path.
            try:
                source = snapshot_download(model_name, local_files_only=True)
            except LocalEntryNotFoundError:
                source = model_name
        _models[model_name] = SentenceTransformer(source)
    model = _models[model_name]
    model.max_seq_length = max_seq_length
    return model


def read_config(config: StageConfig) -> dict[str, int | str]:
    values: dict[str, int | str] = {
        "model_name": config.get("model", default=MODEL_NAME, expected_type=str),
        "embedding_dim": config.get("embedding_dim", default=EMBEDDING_DIM, expected_type=int),
        "max_seq_length": config.get("max_seq_length", default=MAX_SEQ_LENGTH, expected_type=int),
        "batch_size": config.get("batch_size", default=BATCH_SIZE, expected_type=int),
        "shard_count": config.get("shard_count", default=SHARD_COUNT, expected_type=int),
    }
    for key in ("embedding_dim", "max_seq_length", "batch_size", "shard_count"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"features.{key} must be a positive integer")
    if not values["model_name"]:
        raise ValueError("features.model must be non-empty")
    return values


def _shard_bounds(n: int, shard_count: int) -> list[tuple[int, int]]:
    """Contiguous, deterministic shard boundaries over a stable-sorted
    frame — shard i always covers the same doc_ids regardless of machine
    or run, which is what makes a resumed shard actually resumable."""
    base, remainder = divmod(n, shard_count)
    bounds = []
    start = 0
    for i in range(shard_count):
        size = base + (1 if i < remainder else 0)
        bounds.append((start, start + size))
        start += size
    return bounds


@dataclass(frozen=True)
class FeatureShard:
    shard_index: int
    doc_ids: list[int]
    embeddings: npt.NDArray[np.float32]
    content_digest: str


def run_shards(
    df: pd.DataFrame,
    *,
    shard_count: int = SHARD_COUNT,
    batch_size: int = BATCH_SIZE,
    max_seq_length: int = MAX_SEQ_LENGTH,
    model_name: str = MODEL_NAME,
    expected_embedding_dim: int | None = EMBEDDING_DIM,
) -> Iterator[FeatureShard]:
    """Yields one FeatureShard per shard, in order. Each shard is an
    independently verifiable unit of work — the fragmentation PROJECT.md
    §4.5 resumes from after a crash (features -> 3 shards).

    batch_size/max_seq_length are parameters, not just module constants,
    because PROJECT.md §5.3's F3 failure is exactly this stage misconfigured
    with batch_size=4096, max_seq_length=512 — scripts/seed_memory.py (D6)
    needs to be able to actually trigger that, not a stand-in for it."""
    if shard_count < 1 or batch_size < 1 or max_seq_length < 1:
        raise ValueError("shard_count, batch_size, and max_seq_length must be positive")
    model = _get_model(max_seq_length, model_name)
    for shard_index, (start, end) in enumerate(_shard_bounds(len(df), shard_count)):
        chunk = df.iloc[start:end]
        texts = chunk["text"].tolist()
        embeddings = np.asarray(
            model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=False,
            ),
            dtype=np.float32,
        )
        if embeddings.ndim != 2:
            raise ValueError(f"embedding model returned a {embeddings.ndim}-D array, expected 2-D")
        if expected_embedding_dim is not None and embeddings.shape[1] != expected_embedding_dim:
            raise ValueError(
                f"embedding model returned dimension {embeddings.shape[1]}, "
                f"config declares {expected_embedding_dim}"
            )
        digest = hashlib.sha256(canonical_float32_bytes(embeddings)).hexdigest()
        yield FeatureShard(
            shard_index=shard_index,
            doc_ids=chunk["doc_id"].tolist(),
            embeddings=embeddings,
            content_digest=digest,
        )


@dataclass(frozen=True)
class FeaturesArtifact:
    parquet_bytes: bytes
    num_rows: int
    embedding_dim: int


def _write_parquet(doc_ids: list[int], embeddings: npt.NDArray[np.float32]) -> bytes:
    n, dim = embeddings.shape
    flat = pa.array(embeddings.reshape(-1), type=pa.float32())
    embedding_col = pa.FixedSizeListArray.from_arrays(flat, dim)
    table = pa.table({"doc_id": pa.array(doc_ids, type=pa.int64()), "embedding": embedding_col})
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    result: bytes = buf.getvalue().to_pybytes()
    return result


def read_parquet(parquet_bytes: bytes) -> tuple[list[int], npt.NDArray[np.float32]]:
    """Inverse of _write_parquet — stage_train.py reads features back
    through this, not through raw pyarrow, so the schema lives in one
    place."""
    table = pq.read_table(pa.BufferReader(parquet_bytes))
    doc_ids: list[int] = table.column("doc_id").to_pylist()
    embedding_col = table.column("embedding").combine_chunks()
    dim = embedding_col.type.list_size
    flat = np.asarray(embedding_col.flatten(), dtype=np.float32)
    embeddings = flat.reshape(len(doc_ids), dim)
    return doc_ids, embeddings


def run(
    dataset_parquet_bytes: bytes,
    *,
    config: StageConfig | None = None,
    shard_count: int | None = None,
    batch_size: int | None = None,
    max_seq_length: int | None = None,
    model_name: str | None = None,
    embedding_dim: int | None = None,
) -> FeaturesArtifact:
    configured = read_config(config) if config is not None else {}
    shard_count = int(
        shard_count if shard_count is not None else configured.get("shard_count", SHARD_COUNT)
    )
    batch_size = int(
        batch_size if batch_size is not None else configured.get("batch_size", BATCH_SIZE)
    )
    max_seq_length = int(
        max_seq_length
        if max_seq_length is not None
        else configured.get("max_seq_length", MAX_SEQ_LENGTH)
    )
    model_name = str(
        model_name if model_name is not None else configured.get("model_name", MODEL_NAME)
    )
    embedding_dim = int(
        embedding_dim
        if embedding_dim is not None
        else configured.get("embedding_dim", EMBEDDING_DIM)
    )
    df = pd.read_parquet(pa.BufferReader(dataset_parquet_bytes))
    df = df.sort_values("doc_id", kind="stable").reset_index(drop=True)

    all_doc_ids: list[int] = []
    shard_embeddings: list[npt.NDArray[np.float32]] = []
    for shard in run_shards(
        df,
        shard_count=shard_count,
        batch_size=batch_size,
        max_seq_length=max_seq_length,
        model_name=model_name,
        expected_embedding_dim=embedding_dim,
    ):
        all_doc_ids.extend(shard.doc_ids)
        shard_embeddings.append(shard.embeddings)

    embeddings = np.concatenate(shard_embeddings, axis=0)
    parquet_bytes = _write_parquet(all_doc_ids, embeddings)
    return FeaturesArtifact(
        parquet_bytes=parquet_bytes, num_rows=len(all_doc_ids), embedding_dim=embedding_dim
    )
