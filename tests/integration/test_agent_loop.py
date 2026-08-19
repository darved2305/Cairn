"""Integration test for the full agent loop pipeline (agent/loop.py) —
docs/project/PROJECT.md §6.4. See tests/integration/test_agent_remediation.py for the
negative-memory/REMEDIATE_AND_REPLAN tests, split out because they don't
need real S3 or a loaded embedding model and this one does.

On this development machine, real-S3 + a loaded torch model in the same
process crashes at the native DLL level (reproduced with a minimal repro
outside pytest, and confirmed to equally break the pre-existing
tests/integration/test_pipeline_e2e.py, unrelated to this file) — the same
class of local Avast/OpenSSL interception issue documented for
scripts/race.py. This test is expected to pass in an environment where
that conflict doesn't occur (CI, a different machine, or this one on a
different network); the `_require_s3`/`warm_embedding_model` fixtures
below are exactly what every other real-S3 test in this suite already
depends on.
"""

from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest

from cairn.agent.actions import Action
from cairn.agent.loop import run_pipeline
from cairn.config import TrackedConfig
from cairn.storage import s3
from cairn.workload import stage_features

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
    import contextlib

    client = s3.get_client()
    with contextlib.suppress(Exception):
        client.create_bucket(Bucket=BUCKET)


def _config(token: str, *, input_dim: int = 384) -> TrackedConfig:
    snapshot_version = f"agent-loop-test-{token}"
    rows = [
        {"doc_id": i, "text": _TOPICS[i % 4], "target": i % 4, "target_name": f"cat{i % 4}"}
        for i in range(24)
    ]
    s3.put_bytes(
        BUCKET,
        f"datasets/{snapshot_version}/raw.parquet",
        pd.DataFrame(rows).to_parquet(index=False),
    )
    return TrackedConfig(
        {
            "data": {"snapshot_digest": snapshot_version},
            "dataset": {"test_every_nth": 5},
            "features": {
                "model": stage_features.MODEL_NAME,
                "embedding_dim": stage_features.EMBEDDING_DIM,
                "max_seq_length": stage_features.MAX_SEQ_LENGTH,
                "batch_size": 8,
                "shard_count": 1,
            },
            "train": {
                "input_dim": input_dim,
                "hidden_dim": 32,
                "num_labels": 4,
                "epochs": 2,
                "learning_rate": 0.01,
                "batch_size": 8,
            },
            "eval": {"metrics": ["accuracy"]},
        }
    )


def test_full_pipeline_recomputes_then_reuses_by_identity(pool, warm_embedding_model: None) -> None:
    token = uuid.uuid4().hex
    config = _config(token)
    owner_a, owner_b = f"owner-a-{token}", f"owner-b-{token}"

    first = run_pipeline(
        pool,
        config=config,
        source_root="src",
        bucket=BUCKET,
        owner=owner_a,
        host="host-a",
        region="us-east-1",
        target_stage="eval",
    )
    assert [o.stage for o in first] == ["env", "dataset", "features", "checkpoint", "eval"]
    for outcome in first:
        assert outcome.action in (Action.RECOMPUTE, Action.REUSE)
        assert outcome.artifact is not None

    eval_bytes = s3.get_bytes(BUCKET, first[-1].artifact.s3_uri.split("/", 3)[-1])  # type: ignore[union-attr]
    assert b"accuracy" in eval_bytes

    # Same config, same work keys throughout -> every stage is a free
    # identity reuse the second time, no compute repeated.
    second = run_pipeline(
        pool,
        config=_config(token),  # re-uploads the same raw snapshot under the same key; harmless
        source_root="src",
        bucket=BUCKET,
        owner=owner_b,
        host="host-b",
        region="us-east-1",
        target_stage="eval",
    )
    for outcome, prior in zip(second, first, strict=True):
        assert outcome.action == Action.REUSE, f"{outcome.stage} should have been identity-reused"
        assert outcome.artifact is not None and prior.artifact is not None
        assert outcome.artifact.artifact_id == prior.artifact.artifact_id
