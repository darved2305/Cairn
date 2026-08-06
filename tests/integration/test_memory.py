from __future__ import annotations

import uuid

import pytest

from cairn.db import memory
from cairn.db.memory import (
    ChangedKey,
    FailureSignature,
    MatchTier,
    PlanFeatures,
    Remediation,
    tier,
)

pytestmark = pytest.mark.integration


def _embedding(seed: int, dim: int = 1024) -> list[float]:
    """A cheap, deterministic, non-mocked stand-in for a real Titan call —
    this module only needs *some* 1024-dim vector to prove the storage and
    cosine-distance query work; the embedding provider itself is task #7."""
    import random

    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(dim)]


def test_ensure_vector_index_never_raises_and_reports_a_status(pool) -> None:
    status = memory.ensure_vector_index(pool)
    assert isinstance(status.active, bool)
    assert status.detail


def test_record_and_search_round_trips_a_failure_signature(pool) -> None:
    token = uuid.uuid4().hex
    run_id = uuid.uuid4()
    embedding = _embedding(1)

    signature_id = memory.record_failure_signature(
        pool,
        FailureSignature(
            stage="checkpoint",
            error_class=f"IndexError-{token}",
            workload_kind="text-classification",
            traceback_head="IndexError: Target 5 is out of bounds.",
            summary_text=f"label-space mismatch {token}",
            embedding=embedding,
            run_id=run_id,
            wasted_ms=3000,
            num_labels=6,
        ),
    )

    results = memory.search(
        pool, stage="checkpoint", error_class_hint=f"IndexError-{token}", embedding=embedding
    )
    assert len(results) == 1
    assert results[0].signature_id == signature_id
    assert results[0].cosine_distance == pytest.approx(0.0, abs=1e-6)
    assert results[0].structured["num_labels"] == 6
    assert results[0].causal_keys is None  # no remediation recorded yet


def test_search_respects_the_stage_and_error_class_filter(pool) -> None:
    token = uuid.uuid4().hex
    embedding = _embedding(2)

    memory.record_failure_signature(
        pool,
        FailureSignature(
            stage="checkpoint",
            error_class=f"RuntimeError-{token}",
            workload_kind="text-classification",
            traceback_head="RuntimeError: shape mismatch",
            summary_text="embedding dimension mismatch",
            embedding=embedding,
            run_id=uuid.uuid4(),
            wasted_ms=2000,
        ),
    )

    # Different stage: must not match even with an identical embedding.
    results = memory.search(
        pool, stage="eval", error_class_hint=f"RuntimeError-{token}", embedding=embedding
    )
    assert results == []

    # Different error_class: must not match.
    results = memory.search(
        pool, stage="checkpoint", error_class_hint=f"OtherError-{token}", embedding=embedding
    )
    assert results == []


def test_search_with_no_error_class_hint_returns_stage_matches(pool) -> None:
    token = uuid.uuid4().hex
    embedding = _embedding(3)
    memory.record_failure_signature(
        pool,
        FailureSignature(
            stage=f"features-{token}",
            error_class="MemoryError",
            workload_kind="text-classification",
            traceback_head="MemoryError: allocation failed",
            summary_text="bounded memory exhaustion",
            embedding=embedding,
            run_id=uuid.uuid4(),
            wasted_ms=1500,
        ),
    )
    results = memory.search(
        pool, stage=f"features-{token}", error_class_hint=None, embedding=embedding
    )
    assert len(results) == 1


def test_remediation_causal_keys_flow_into_search_and_tiering(pool) -> None:
    token = uuid.uuid4().hex
    embedding = _embedding(4)

    signature_id = memory.record_failure_signature(
        pool,
        FailureSignature(
            stage="checkpoint",
            error_class=f"RuntimeError-{token}",
            workload_kind="text-classification",
            traceback_head="RuntimeError: mat1 and mat2 shapes cannot be multiplied",
            summary_text="embedding dimension mismatch — mpnet",
            embedding=embedding,
            run_id=uuid.uuid4(),
            wasted_ms=60_000,
            num_labels=4,
            embedding_dim=384,
            framework_version="2.5.0",
            instance_kind="fargate-2vcpu-4gib",
        ),
    )
    memory.record_remediation(
        pool,
        Remediation(
            signature_id=signature_id,
            changed_keys=[ChangedKey(key="embedding_dim", from_value=384, to_value=768)],
            rationale="mpnet is 768-d, classifier.input_dim must match",
            succeeded=True,
        ),
    )

    results = memory.search(
        pool, stage="checkpoint", error_class_hint=f"RuntimeError-{token}", embedding=embedding
    )
    assert len(results) == 1
    assert results[0].causal_keys == frozenset({"embedding_dim"})

    # A near-miss config with the SAME wrong embedding_dim -> strong_semantic, blocks.
    candidate = PlanFeatures(
        stage="checkpoint",
        error_class_hint=f"RuntimeError-{token}",
        structured={
            "num_labels": 4,
            "embedding_dim": 384,
            "framework_version": "2.9.0",
            "instance_kind": "fargate-2vcpu-4gib",
        },
    )
    verdict = tier(results[0], candidate)
    assert verdict.tier == MatchTier.STRONG_SEMANTIC

    # After applying the remediation (embedding_dim -> 768), the causal key
    # no longer agrees -> falls back to at most weak, never blocks.
    fixed_candidate = PlanFeatures(
        stage="checkpoint",
        error_class_hint=f"RuntimeError-{token}",
        structured={**candidate.structured, "embedding_dim": 768},
    )
    verdict_after_fix = tier(results[0], fixed_candidate)
    assert verdict_after_fix.tier in (MatchTier.WEAK, MatchTier.NONE)
