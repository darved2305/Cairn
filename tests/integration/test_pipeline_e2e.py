"""Full pipeline, end-to-end, through real storage: env -> dataset ->
features -> checkpoint -> eval, each stage's output actually written to
and read back from S3 (MinIO locally), not passed in-memory between
stages. PLAN.md D3 exit criterion: "full pipeline runs locally in Docker
end-to-end."

Uses a small synthetic corpus rather than the full ~3200-document vendored
snapshot — this test is proving the five stages and the storage layer
wire together correctly, which doesn't need PROJECT.md's real dataset
size to be true; scripts/vendor_dataset.py + a real bucket is what you'd
point this at for the full-size run.
"""

from __future__ import annotations

import contextlib
import os

import pandas as pd
import pytest

from cairn.storage import s3
from cairn.workload import (
    determinism,
    stage_dataset,
    stage_env,
    stage_eval,
    stage_features,
    stage_train,
)

pytestmark = pytest.mark.integration

BUCKET = "cairn-test"
_TOPICS = [
    "The rocket launched into orbit carrying a new communications satellite payload.",
    "The mechanic replaced the timing belt and checked the brake pads carefully.",
    "The graphics card renders millions of triangles at a smooth frame rate.",
    "Diplomats met for hours to discuss the ceasefire terms and regional tensions.",
]


@pytest.fixture(scope="module", autouse=True)
def _require_s3() -> None:
    if not os.environ.get("CAIRN_S3_ENDPOINT_URL"):
        pytest.skip("CAIRN_S3_ENDPOINT_URL not set — needs a local MinIO or real S3 bucket")
    client = s3.get_client()
    # bucket-already-exists is fine; anything else surfaces downstream anyway
    with contextlib.suppress(Exception):
        client.create_bucket(Bucket=BUCKET)


def _synthetic_raw_snapshot(n: int = 25) -> bytes:
    rows = [
        {"doc_id": i, "text": _TOPICS[i % 4], "target": i % 4, "target_name": f"cat{i % 4}"}
        for i in range(n)
    ]
    return pd.DataFrame(rows).to_parquet(index=False)


def test_full_pipeline_through_real_storage(warm_embedding_model: None) -> None:
    determinism.apply()

    # env
    env_manifest = stage_env.run()
    assert env_manifest.env_fingerprint

    # dataset — "vendored" snapshot uploaded directly for this test instead
    # of going through scripts/vendor_dataset.py's live corpus fetch.
    raw_uri_result = s3.put_bytes(BUCKET, "datasets/test-v1/raw.parquet", _synthetic_raw_snapshot())
    assert raw_uri_result.startswith("s3://")
    raw_bytes = s3.get_bytes(BUCKET, "datasets/test-v1/raw.parquet")

    dataset_artifact = stage_dataset.run(raw_bytes)
    dataset_put = s3.put_content_addressed(BUCKET, "dataset", dataset_artifact.parquet_bytes)
    dataset_bytes = s3.get_bytes(BUCKET, dataset_put.key)
    assert dataset_artifact.num_train > 0
    assert dataset_artifact.num_test > 0

    # features
    features_artifact = stage_features.run(dataset_bytes, shard_count=2)
    features_put = s3.put_content_addressed(BUCKET, "features", features_artifact.parquet_bytes)
    features_bytes = s3.get_bytes(BUCKET, features_put.key)
    assert features_artifact.num_rows == dataset_artifact.num_rows
    assert features_artifact.embedding_dim == stage_features.EMBEDDING_DIM

    # checkpoint
    joined = stage_train.join_dataset_and_features(dataset_bytes, features_bytes)
    checkpoint_artifact = stage_train.run(joined, epochs=2)
    checkpoint_put = s3.put_content_addressed(
        BUCKET, "checkpoint", checkpoint_artifact.state_dict_bytes
    )
    checkpoint_bytes = s3.get_bytes(BUCKET, checkpoint_put.key)

    # eval
    eval_artifact = stage_eval.run(joined, checkpoint_bytes)
    s3.put_content_addressed(BUCKET, "eval", eval_artifact.json_bytes)

    assert 0.0 <= eval_artifact.accuracy <= 1.0
    assert 0.0 <= eval_artifact.macro_f1 <= 1.0
    assert eval_artifact.num_eval_examples == dataset_artifact.num_test

    # Re-uploading the same content must be a genuine no-op (idempotency —
    # PROJECT.md §4.2), not a second real write.
    replay = s3.put_content_addressed(BUCKET, "checkpoint", checkpoint_artifact.state_dict_bytes)
    assert replay.already_existed
    assert replay.key == checkpoint_put.key
