"""Integration tests for agent/loop.py's pre-flight negative-memory check
and REMEDIATE_AND_REPLAN — the exact "recompute key -> re-query memory ->
then claim" loop the D5/D6 audit found missing (only the underlying
primitives, memory.search/tier, were previously proven wired end-to-end).

Two rules are pinned here, at the loop level:

1. A remembered failure with no *verified* remediation on record is
   advisory-only and never blocks. PROJECT.md §4.1 defines both blocking
   tiers in terms of the recorded remediation's changed_keys (those are
   what make a structured feature *causal*), and db/memory.py's search
   only joins `succeeded` remediations — so a signature carrying nothing
   but an unverified LLM-authored proposal (succeeded=False) has no
   causal keys, tiers at most WEAK, and the loop must proceed. Pinned at
   the tier level by tests/unit/test_memory.py's
   test_strong_semantic_requires_a_recorded_remediation; pinned at the
   loop level here by proving run_stage walks straight past the memory
   check into a (pre-seeded) identity reuse with zero compute spent.

2. A remembered failure *with* a verified remediation blocks and is
   auto-fixed: REMEDIATE_AND_REPLAN applies the recorded changed_keys,
   re-plans, re-queries memory against the modified plan, and only then
   claims (PLAN.md D6's exit bar, proven at the tier level in
   test_d6_exit.py).

Deliberately touches only the live CockroachDB cluster — no S3, no boto3
client, no loaded embedding model (CAIRN_NO_LLM keeps embeddings on the
offline hashlib-based fallback). This process also imports
`cairn.agent.loop`, which imports torch; a real boto3 API call in the same
process as an imported torch crashes at the native DLL level on this
development machine (see tests/integration/test_agent_loop.py's docstring)
— these tests sidestep that by construction rather than working around it,
since nothing here needs S3 anyway: both tests pre-seed a SUCCEEDED
artifact through the real claim protocol so `run_stage` hits identity
reuse instead of a real training run (checkpoint's own real compute is
proven separately, in test_agent_loop.py and test_d5_exit.py).
"""

from __future__ import annotations

import uuid

import pytest

from cairn.agent.actions import Action
from cairn.agent.loop import run_stage
from cairn.config import TrackedConfig
from cairn.db import claims, memory
from cairn.db.environments import ensure_environment
from cairn.db.graph import ArtifactInput
from cairn.embeddings import default_provider
from cairn.planner import plan_pipeline
from cairn.workload import stage_env, stage_features

pytestmark = pytest.mark.integration


def _config(*, snapshot_version: str, input_dim: int) -> TrackedConfig:
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


def _plan_for(token: str, config: TrackedConfig):  # noqa: ANN201 - local test helper
    environment = stage_env.run()
    plan = plan_pipeline(
        config,
        source_root="src",
        env_fingerprint=environment.env_fingerprint,
        data_fingerprint=f"agent-remediation-test-{token}",
    )
    return environment, plan


def test_unverified_remediation_is_advisory_only_and_never_blocks(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A matching failure signature is on record for this exact
    misconfiguration, carrying only an *unverified* LLM-authored
    remediation proposal (succeeded=False — what classify/llm.py's
    author_remediation produces until a real run using it succeeds). The
    loop must NOT block: with no verified remediation there are no known
    causal keys, both blocking tiers are unreachable (PROJECT.md §4.1),
    and the match is advisory-only. run_stage therefore proceeds past the
    pre-flight check into claims.acquire — here hitting the SUCCEEDED
    claim pre-seeded under this exact work_key, i.e. identity reuse with
    zero compute spent, which is what proves the memory check did not
    block. A wrong input_dim the system has never seen fail must not
    block either; the other test here covers that implicitly (its random
    wrong value never false-blocks on the *first*, unremediated attempt).
    """

    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    token = uuid.uuid4().hex
    embedding_dim = stage_features.EMBEDDING_DIM
    wrong_input_dim = 100000 + (uuid.uuid4().int % 50000)
    config = _config(snapshot_version=f"agent-remediation-test-{token}", input_dim=wrong_input_dim)
    environment, plan = _plan_for(token, config)
    owner = f"owner-{token}"

    traceback_head = (
        f"ValueError: training input dimension is {embedding_dim}, "
        f"configured input_dim is {wrong_input_dim}"
    )
    summary_text = (
        f"checkpoint stage failed with {traceback_head} — the embedding "
        f"model produces {embedding_dim}-d vectors but "
        f"train.input_dim was still {wrong_input_dim}"
    )
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="checkpoint",
            error_class="ValueError",
            workload_kind="text-classification",
            traceback_head=traceback_head,
            summary_text=summary_text,
            embedding=default_provider().embed(summary_text),
            run_id=uuid.uuid4(),
            wasted_ms=1000,
            embedding_dim=embedding_dim,
            framework="pytorch",
            instance_kind="fargate-2vcpu-4gib",
            vcpu=2.0,
            mem_mib=4096,
        ),
    )
    # The unverified proposal is on record and still must not block —
    # classify/llm.py's module docstring: it "only becomes causal ... once
    # a real run using it actually succeeds".
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[
                memory.ChangedKey(key="embedding_dim", from_value=None, to_value=embedding_dim)
            ],
            rationale="unverified LLM proposal: train.input_dim should equal embedding_dim",
            succeeded=False,
        ),
    )

    # Pre-seed a SUCCEEDED checkpoint artifact under the work_key this
    # exact (wrong) config produces, via the real claim protocol — so the
    # loop's post-memory-check acquire resolves to identity reuse instead
    # of a real training run.
    ensure_environment(
        pool,
        env_fingerprint=environment.env_fingerprint,
        image_digest=environment.image_digest,
        python_version=environment.python_version,
        deps=environment.deps,
        torch_threads=environment.torch_threads,
    )
    work_key = plan.by_stage()["checkpoint"].work_key.value
    run_id = uuid.uuid4()
    seed_claim = claims.acquire(pool, work_key, "checkpoint", owner, "host-a", "us-east-1", run_id)
    assert seed_claim.won
    artifact_id = f"artifact-{token}"
    record = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage="checkpoint",
        work_key=work_key,
        s3_uri=f"s3://cairn-test/checkpoint/{artifact_id}",
        size_bytes=64,
        env_fingerprint=environment.env_fingerprint,
        produced_by_run=run_id,
        duration_ms=1,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    assert claims.complete(
        pool,
        work_key,
        owner,
        seed_claim.fence,
        record,
        inputs=[ArtifactInput("env", "environment", environment.env_fingerprint)],
    )

    outcome = run_stage(
        pool,
        plan=plan,
        stage_name="checkpoint",
        config=config,
        bucket="unused",
        owner=owner,
        host="host-a",
        region="us-east-1",
        run_id=uuid.uuid4(),
        environment=environment,
        upstream={},
    )
    assert outcome.action == Action.REUSE, (
        "an advisory-only (WEAK) memory match must never block — the loop "
        "should have proceeded to the claim and adopted the seeded artifact"
    )
    assert outcome.artifact is not None
    assert outcome.artifact.artifact_id == artifact_id


def test_checkpoint_shape_mismatch_remediation_unblocks_via_identity_reuse(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    token = uuid.uuid4().hex
    embedding_dim = stage_features.EMBEDDING_DIM  # 384, real
    wrong_input_dim = 200000 + (uuid.uuid4().int % 50000)

    config = _config(snapshot_version=f"agent-remediation-test-{token}", input_dim=wrong_input_dim)
    environment, plan = _plan_for(token, config)
    owner = f"owner-{token}"

    # --- seed the matching signature + a verified remediation, embedding
    # the exact summary_text agent/loop.py's own pre-flight check
    # constructs, so cosine distance to it is ~0 regardless of provider ---
    traceback_head = (
        f"ValueError: training input dimension is {embedding_dim}, "
        f"configured input_dim is {wrong_input_dim}"
    )
    summary_text = (
        f"checkpoint stage failed with {traceback_head} — the embedding "
        f"model produces {embedding_dim}-d vectors but "
        f"train.input_dim was still {wrong_input_dim}"
    )
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="checkpoint",
            error_class="ValueError",
            workload_kind="text-classification",
            traceback_head=traceback_head,
            summary_text=summary_text,
            embedding=default_provider().embed(summary_text),
            run_id=uuid.uuid4(),
            wasted_ms=1000,
            embedding_dim=embedding_dim,
            framework="pytorch",
            instance_kind="fargate-2vcpu-4gib",
            vcpu=2.0,
            mem_mib=4096,
        ),
    )
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[
                memory.ChangedKey(
                    key="embedding_dim", from_value=wrong_input_dim, to_value=embedding_dim
                )
            ],
            rationale="train.input_dim must equal the embedding model's actual output dimension",
            verified_run_id=uuid.uuid4(),
            succeeded=True,
        ),
    )

    # --- pre-seed a SUCCEEDED checkpoint artifact under the work_key the
    # *corrected* config (train.input_dim == embedding_dim) would produce,
    # via the real claim protocol — so REMEDIATE_AND_REPLAN's recursive
    # run_stage call hits identity reuse, not a real training run ---
    fixed_config = _config(
        snapshot_version=f"agent-remediation-test-{token}", input_dim=embedding_dim
    )
    fixed_plan = plan_pipeline(
        fixed_config,
        source_root="src",
        env_fingerprint=environment.env_fingerprint,
        data_fingerprint=f"agent-remediation-test-{token}",
    )
    fixed_work_key = fixed_plan.by_stage()["checkpoint"].work_key.value

    ensure_environment(
        pool,
        env_fingerprint=environment.env_fingerprint,
        image_digest=environment.image_digest,
        python_version=environment.python_version,
        deps=environment.deps,
        torch_threads=environment.torch_threads,
    )
    run_id = uuid.uuid4()
    seed_claim = claims.acquire(
        pool, fixed_work_key, "checkpoint", owner, "host-a", "us-east-1", run_id
    )
    assert seed_claim.won
    fake_bytes = f"placeholder-checkpoint-{token}".encode()
    artifact_id = f"artifact-{token}"
    record = claims.ArtifactRecord(
        artifact_id=artifact_id,
        stage="checkpoint",
        work_key=fixed_work_key,
        s3_uri=f"s3://cairn-test/checkpoint/{artifact_id}",
        size_bytes=len(fake_bytes),
        env_fingerprint=environment.env_fingerprint,
        produced_by_run=run_id,
        duration_ms=1,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )
    assert claims.complete(
        pool,
        fixed_work_key,
        owner,
        seed_claim.fence,
        record,
        inputs=[ArtifactInput("env", "environment", environment.env_fingerprint)],
    )

    outcome = run_stage(
        pool,
        plan=plan,
        stage_name="checkpoint",
        config=config,
        bucket="unused",
        owner=owner,
        host="host-a",
        region="us-east-1",
        run_id=uuid.uuid4(),
        environment=environment,
        upstream={},
    )
    assert outcome.action == Action.REMEDIATE_AND_REPLAN
    assert outcome.artifact is not None
    assert outcome.artifact.artifact_id == artifact_id
