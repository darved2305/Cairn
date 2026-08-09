from __future__ import annotations

import uuid
from datetime import UTC, datetime

from cairn.db import memory
from cairn.db.memory import BLOCKING_TIERS, Match, MatchTier, PlanFeatures, tier

_NOW = datetime(2026, 8, 7, tzinfo=UTC)


def _match(
    *,
    cosine_distance: float,
    error_class: str = "IndexError",
    stage: str = "checkpoint",
    causal_keys: frozenset[str] | None = None,
    structured: dict[str, object] | None = None,
) -> Match:
    return Match(
        signature_id=uuid.uuid4(),
        stage=stage,
        error_class=error_class,
        traceback_head="IndexError: Target 5 is out of bounds.",
        summary_text="label-space mismatch",
        structured=structured or {},
        wasted_ms=3000,
        created_at=_NOW,
        cosine_distance=cosine_distance,
        causal_keys=causal_keys,
    )


def _candidate(
    *,
    error_class_hint: str | None = "IndexError",
    stage: str = "checkpoint",
    structured: dict[str, object] | None = None,
) -> PlanFeatures:
    return PlanFeatures(stage=stage, error_class_hint=error_class_hint, structured=structured or {})


def test_weak_match_never_blocks() -> None:
    match = _match(cosine_distance=0.30)
    result = tier(match, _candidate())
    assert result.tier == MatchTier.WEAK
    assert result.tier not in BLOCKING_TIERS


def test_beyond_weak_threshold_is_no_match() -> None:
    match = _match(cosine_distance=0.9)
    result = tier(match, _candidate())
    assert result.tier == MatchTier.NONE
    assert result.tier not in BLOCKING_TIERS


def test_strong_semantic_requires_a_recorded_remediation() -> None:
    # cosine distance and stage/error_class agreement alone are NOT enough
    # without knowing which structured feature was actually causal.
    match = _match(
        cosine_distance=0.05,
        causal_keys=None,
        structured={"num_labels": 4},
    )
    result = tier(match, _candidate(structured={"num_labels": 4}))
    assert result.tier == MatchTier.WEAK, "no remediation on record -> can never exceed weak"


def test_strong_semantic_fires_with_causal_agreement_and_close_cosine() -> None:
    match = _match(
        cosine_distance=0.11,
        causal_keys=frozenset({"num_labels"}),
        structured={"num_labels": 4, "framework_version": "2.5.0"},
    )
    candidate = _candidate(structured={"num_labels": 4, "framework_version": "2.9.0"})
    result = tier(match, candidate)
    assert result.tier == MatchTier.STRONG_SEMANTIC
    assert result.tier in BLOCKING_TIERS


def test_strong_semantic_does_not_fire_when_causal_key_disagrees() -> None:
    match = _match(
        cosine_distance=0.05,
        causal_keys=frozenset({"num_labels"}),
        structured={"num_labels": 4},
    )
    candidate = _candidate(structured={"num_labels": 6})  # the causal key disagrees
    result = tier(match, candidate)
    assert result.tier == MatchTier.WEAK


def test_exact_requires_all_causal_keys_plus_framework_and_instance_agreement() -> None:
    structured = {
        "num_labels": 4,
        "embedding_dim": 384,
        "framework_version": "2.5.0",
        "instance_kind": "fargate-2vcpu-4gib",
    }
    match = _match(
        cosine_distance=0.0, causal_keys=frozenset({"num_labels"}), structured=structured
    )
    candidate = _candidate(structured=structured)
    result = tier(match, candidate)
    assert result.tier == MatchTier.EXACT
    assert result.tier in BLOCKING_TIERS


def test_exact_does_not_fire_when_instance_kind_differs() -> None:
    structured = {
        "num_labels": 4,
        "framework_version": "2.5.0",
        "instance_kind": "fargate-2vcpu-4gib",
    }
    match = _match(
        cosine_distance=0.0, causal_keys=frozenset({"num_labels"}), structured=structured
    )
    candidate = _candidate(
        structured={**structured, "instance_kind": "fargate-4vcpu-8gib"},
    )
    result = tier(match, candidate)
    # cosine=0.0 and the causal key still agrees, so this falls to strong_semantic, not none.
    assert result.tier == MatchTier.STRONG_SEMANTIC


def test_exact_does_not_fire_when_a_causal_key_is_missing_from_agreement() -> None:
    structured = {
        "num_labels": 4,
        "batch_size": 32,
        "framework_version": "2.5.0",
        "instance_kind": "x",
    }
    match = _match(
        cosine_distance=0.0,
        causal_keys=frozenset({"num_labels", "batch_size"}),
        structured=structured,
    )
    candidate = _candidate(structured={**structured, "batch_size": 64})
    result = tier(match, candidate)
    assert result.tier != MatchTier.EXACT


def test_different_error_class_never_matches_beyond_weak() -> None:
    match = _match(
        cosine_distance=0.02,
        error_class="RuntimeError",
        causal_keys=frozenset({"input_dim"}),
        structured={"input_dim": 384},
    )
    candidate = _candidate(error_class_hint="IndexError", structured={"input_dim": 384})
    result = tier(match, candidate)
    assert result.tier == MatchTier.WEAK


def test_causal_key_extraction_rejects_legacy_mismatched_provenance() -> None:
    structured = {"embedding_dim": 384, "input_dim": 768}

    # This is the malformed shape produced before input_dim had its own
    # column: the key was called embedding_dim but its from-value actually
    # described the classifier input. It must never become blocking evidence.
    legacy = [{"key": "embedding_dim", "from": 768, "to": 384}]
    assert memory._extract_causal_keys(legacy, {"embedding_dim": 384}) is None

    corrected = [{"key": "train.input_dim", "from": 768, "to": 384}]
    assert memory._extract_causal_keys(corrected, structured) == frozenset(
        {"input_dim", "embedding_dim"}
    )


def test_causal_key_extraction_rejects_wrong_from_value() -> None:
    structured = {"embedding_dim": 384, "input_dim": 999}
    remediation = [{"key": "train.input_dim", "from": 768, "to": 384}]
    assert memory._extract_causal_keys(remediation, structured) is None
