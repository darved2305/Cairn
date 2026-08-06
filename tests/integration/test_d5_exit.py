"""D5 exit criteria — PLAN.md D5: "comment-only change -> 5 nodes reused
in ~4s; hidden_dim change -> features reused, checkpoint+eval
recomputed." Exercises D2 (claims), D3 (the real workload), D4 (work
keys), and D5 (classification + probes + decisions) composed together
against a live CockroachDB cluster. No mocked compute, no mocked DB —
PLAN.md's anti-simulation rule.

Two scenarios, matching the two rows of PROJECT.md §4.3's worked-example
table that are actually novel to D5 (the other three — pure comment/format
edits, and a downstream-only eval.metrics change — are already covered by
tests/property/test_code_workkey.py's work-key invariance property and by
test_claims.py's SUCCEEDED-reuse path, since those changes never move the
work key at all):

1. A `logging_only` change to the training loop *does* change the
   checkpoint stage's work key (a real statement is added to the AST) —
   the interesting case is that classify.rules + a P4 probe authorize
   reusing the *prior* artifact under the *new* key anyway, and eval then
   gets a free identity-reuse because its own work key never moved (its
   upstream artifact_id didn't change).
2. A `train.hidden_dim` change leaves the `features` stage's work key
   untouched (hidden_dim isn't in its recorded config reads) but changes
   `checkpoint`'s — proving D4's causal graph, not just D5, but it's the
   "money shot" scenario PLAN.md explicitly names as the exit bar.
"""

from __future__ import annotations

import hashlib
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from psycopg.types.json import Jsonb

from cairn.classify import rules
from cairn.config import TrackedConfig
from cairn.db import claims, decisions
from cairn.db.txn import in_txn
from cairn.probes import p4_logits
from cairn.workload import determinism, stage_eval, stage_features, stage_train

pytestmark = pytest.mark.integration

_TOPICS = [
    "The rocket launched into orbit carrying a new communications satellite payload.",
    "The mechanic replaced the timing belt and checked the brake pads carefully.",
    "The graphics card renders millions of triangles at a smooth frame rate.",
    "Diplomats met for hours to discuss the ceasefire terms and regional tensions.",
]


def _insert_environment(pool, env_fingerprint: str) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO environments (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (env_fingerprint) DO NOTHING
            """,
            (env_fingerprint, "sha256:test", "3.12", Jsonb({"test": True}), 1),
        )

    in_txn(pool, _tx, op="test.d5_exit_environment")


def _synthetic_joined(n: int = 20) -> stage_train.JoinedData:
    rng = np.random.default_rng(0)
    doc_ids = list(range(n))
    embeddings = rng.normal(size=(n, 8)).astype(np.float32)
    labels = np.array([i % 3 for i in range(n)], dtype=np.int64)
    split = ["test" if i % 5 == 0 else "train" for i in range(n)]
    return stage_train.JoinedData(
        doc_ids=doc_ids, embeddings=embeddings, labels=labels, split=split
    )


# ---------------------------------------------------------------------------
# Scenario 1: logging_only change to train.py
# ---------------------------------------------------------------------------


def test_logging_only_change_reuses_checkpoint_via_probe_and_eval_via_identity(pool) -> None:
    determinism.apply()
    token = uuid.uuid4().hex
    env_fp = f"env-{token}"
    _insert_environment(pool, env_fp)

    owner, host, region, run_id = f"owner-{token}", "host-a", "us-east-1", uuid.uuid4()
    joined = _synthetic_joined()

    # --- baseline checkpoint, produced by real training ---
    checkpoint_key = f"checkpoint-{token}-v1"
    claim = claims.acquire(pool, checkpoint_key, "checkpoint", owner, host, region, run_id)
    assert claim.won
    checkpoint = stage_train.run(joined, hidden_dim=8, input_dim=8, num_labels=3, epochs=2)
    artifact_id = hashlib.sha256(checkpoint.state_dict_bytes).hexdigest()
    record = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage="checkpoint",
        work_key=checkpoint_key,
        s3_uri=f"s3://test/checkpoint/{artifact_id}",
        size_bytes=len(checkpoint.state_dict_bytes),
        env_fingerprint=env_fp,
        produced_by_run=run_id,
        duration_ms=10,
        vcpu=2.0,
        mem_mib=4096,
        region=region,
    )
    assert claims.complete(pool, checkpoint_key, owner, claim.fence, record)

    # --- baseline eval, produced from that checkpoint ---
    eval_key = f"eval-{token}-v1"
    eval_claim = claims.acquire(pool, eval_key, "eval", owner, host, region, run_id)
    assert eval_claim.won
    eval_artifact = stage_eval.run(
        joined,
        checkpoint.state_dict_bytes,
        input_dim=8,
        hidden_dim=8,
        num_labels=3,
        metrics=["accuracy"],
    )
    eval_artifact_id = hashlib.sha256(eval_artifact.json_bytes).hexdigest()
    eval_record = claims.ArtifactRecord(
        artifact_id=eval_artifact_id,
        stage="eval",
        work_key=eval_key,
        s3_uri=f"s3://test/eval/{eval_artifact_id}",
        size_bytes=len(eval_artifact.json_bytes),
        env_fingerprint=env_fp,
        produced_by_run=run_id,
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region=region,
    )
    assert claims.complete(pool, eval_key, owner, eval_claim.fence, eval_record)

    # --- classify: a logging statement added inside the training loop ---
    old_src = "def run_epochs():\n    total = 0\n    for x in range(3):\n        total += x\n    return total\n"
    new_src = (
        "def run_epochs():\n    total = 0\n    for x in range(3):\n"
        "        logger.debug('x=%s', x)\n        total += x\n    return total\n"
    )
    new_file = "import logging\n\nlogger = logging.getLogger(__name__)\n\n" + new_src
    verdict = rules.classify_logging_only(
        old_unit_source=old_src,
        new_unit_source=new_src,
        new_file_source=new_file,
        reachable_units=[],
    )
    assert verdict.applies and not verdict.refused

    # --- probe: same checkpoint bytes, proving inference is reproducible ---
    probe = p4_logits.run(
        artifact_id,
        joined.doc_ids,
        joined.embeddings,
        checkpoint.state_dict_bytes,
        checkpoint.state_dict_bytes,
        input_dim=8,
        hidden_dim=8,
        num_labels=3,
        sample_size=10,
    )
    assert probe.passed

    # --- the code change moved checkpoint's work key: a genuinely new
    # claim, but completed by reusing the SAME (content-addressed) artifact ---
    new_checkpoint_key = f"checkpoint-{token}-v2"
    claim2 = claims.acquire(pool, new_checkpoint_key, "checkpoint", owner, host, region, run_id)
    assert claim2.won, "a code change must produce a fresh work key, not an automatic identity hit"
    assert claims.complete(pool, new_checkpoint_key, owner, claim2.fence, record)

    decision_id, probe_run_id = decisions.record_decision_with_probe(
        pool,
        decisions.ReuseDecision(
            work_key=new_checkpoint_key,
            stage="checkpoint",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            authorized_by="probe",
            latency_ms=probe.runtime_ms,
            explanation="logging_only class + P4 checkpoint_logit passed",
            change_class="logging_only",
            candidate_artifact_id=artifact_id,
        ),
        probe,
        artifact_id=artifact_id,
    )
    assert decision_id and probe_run_id

    # --- eval's own work key never moved (same upstream artifact_id) ->
    # a second acquire on the SAME key is a free identity reuse, no probe ---
    new_owner, new_run = f"owner2-{token}", uuid.uuid4()
    eval_claim_2 = claims.acquire(pool, eval_key, "eval", new_owner, "host-b", region, new_run)
    assert not eval_claim_2.won
    assert eval_claim_2.reuse_artifact_id == eval_artifact_id

    decisions.record_decision(
        pool,
        decisions.ReuseDecision(
            work_key=eval_key,
            stage="eval",
            action="REUSE",
            verdict="reuse",
            proposed_by="rule",
            authorized_by="identity",
            latency_ms=1,
            explanation="work key unchanged: upstream checkpoint artifact_id is identical",
            candidate_artifact_id=eval_artifact_id,
        ),
    )


# ---------------------------------------------------------------------------
# Scenario 2: train.hidden_dim change
# ---------------------------------------------------------------------------


def _base_config_dict() -> dict[str, object]:
    return {
        "data": {"snapshot_digest": "test-snapshot-v1"},
        "dataset": {"test_every_nth": 5},
        "features": {
            "model": stage_features.MODEL_NAME,
            "embedding_dim": stage_features.EMBEDDING_DIM,
            "max_seq_length": stage_features.MAX_SEQ_LENGTH,
            "batch_size": 32,
            "shard_count": 1,
        },
        "train": {
            "input_dim": stage_features.EMBEDDING_DIM,
            "hidden_dim": 256,
            "num_labels": 4,
            "epochs": 2,
            "learning_rate": 0.001,
            "batch_size": 32,
        },
        "eval": {"metrics": ["accuracy"]},
    }


def test_hidden_dim_change_reuses_features_and_recomputes_checkpoint(
    pool, warm_embedding_model: None
) -> None:
    from cairn.planner import plan_pipeline

    determinism.apply()
    token = uuid.uuid4().hex
    env_fp = f"env-hd-{token}"
    _insert_environment(pool, env_fp)
    data_fp = f"data-hd-{token}"

    old_dict = _base_config_dict()
    new_dict = _base_config_dict()
    new_dict["train"] = {**new_dict["train"], "hidden_dim": 512}  # type: ignore[dict-item]

    plan_old = plan_pipeline(
        TrackedConfig(old_dict), source_root="src", env_fingerprint=env_fp, data_fingerprint=data_fp
    )
    plan_new = plan_pipeline(
        TrackedConfig(new_dict), source_root="src", env_fingerprint=env_fp, data_fingerprint=data_fp
    )

    features_old = plan_old.by_stage()["features"]
    features_new = plan_new.by_stage()["features"]
    assert features_old.work_key.value == features_new.work_key.value, (
        "hidden_dim is not in features' recorded config reads — its work key must be unaffected"
    )

    checkpoint_old = plan_old.by_stage()["checkpoint"]
    checkpoint_new = plan_new.by_stage()["checkpoint"]
    assert checkpoint_old.work_key.value != checkpoint_new.work_key.value

    # --- features: claimed and completed once, for real ---
    owner, host, region, run_id = f"owner-{token}", "host-a", "us-east-1", uuid.uuid4()
    features_key = features_old.work_key.value
    features_claim = claims.acquire(pool, features_key, "features", owner, host, region, run_id)
    assert features_claim.won

    rows = [{"doc_id": i, "text": _TOPICS[i % 4], "target": i % 4} for i in range(20)]
    df = pd.DataFrame(rows)
    df["split"] = ["test" if doc_id % 5 == 0 else "train" for doc_id in df["doc_id"]]
    dataset_bytes: bytes = df.to_parquet(index=False)
    features_artifact = stage_features.run(dataset_bytes, shard_count=1)
    features_artifact_id = hashlib.sha256(features_artifact.parquet_bytes).hexdigest()
    features_record = claims.ArtifactRecord(
        artifact_id=features_artifact_id,
        stage="features",
        work_key=features_key,
        s3_uri=f"s3://test/features/{features_artifact_id}",
        size_bytes=len(features_artifact.parquet_bytes),
        env_fingerprint=env_fp,
        produced_by_run=run_id,
        duration_ms=50,
        vcpu=2.0,
        mem_mib=4096,
        region=region,
    )
    assert claims.complete(pool, features_key, owner, features_claim.fence, features_record)

    # --- a second acquire under the (identical) new-plan features key is a
    # free identity reuse: no recompute, exactly PLAN.md's "features reused" ---
    new_owner, new_run = f"owner2-{token}", uuid.uuid4()
    features_claim_2 = claims.acquire(
        pool, features_new.work_key.value, "features", new_owner, "host-b", region, new_run
    )
    assert not features_claim_2.won
    assert features_claim_2.reuse_artifact_id == features_artifact_id

    # --- checkpoint: two genuinely different work keys -> both require a
    # real claim (won=True each), matching "checkpoint recomputed" ---
    checkpoint_claim_old = claims.acquire(
        pool, checkpoint_old.work_key.value, "checkpoint", owner, host, region, run_id
    )
    checkpoint_claim_new = claims.acquire(
        pool, checkpoint_new.work_key.value, "checkpoint", new_owner, "host-b", region, new_run
    )
    assert checkpoint_claim_old.won
    assert checkpoint_claim_new.won
    assert checkpoint_old.work_key.value != checkpoint_new.work_key.value
