from __future__ import annotations

import uuid
from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from cairn.db.memory import BLOCKING_TIERS, Match, MatchTier, PlanFeatures, tier

_NOW = datetime(2026, 8, 7, tzinfo=UTC)

# Rank order matches PROJECT.md §4.1's table: exact is the strongest tier,
# then strong_semantic, then weak, then no match at all.
_RANK = {MatchTier.EXACT: 3, MatchTier.STRONG_SEMANTIC: 2, MatchTier.WEAK: 1, MatchTier.NONE: 0}


def _match(cosine_distance: float, *, causal: bool) -> Match:
    structured = {"num_labels": 4, "framework_version": "2.5.0", "instance_kind": "x"}
    return Match(
        signature_id=uuid.uuid4(),
        stage="checkpoint",
        error_class="IndexError",
        traceback_head="head",
        summary_text="summary",
        structured=structured,
        wasted_ms=1000,
        created_at=_NOW,
        cosine_distance=cosine_distance,
        causal_keys=frozenset({"num_labels"}) if causal else None,
    )


_CANDIDATE = PlanFeatures(
    stage="checkpoint",
    error_class_hint="IndexError",
    structured={"num_labels": 4, "framework_version": "2.5.0", "instance_kind": "x"},
)


@settings(max_examples=500)
@given(
    a=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    b=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    causal=st.booleans(),
)
def test_tier_is_monotone_in_cosine_distance(a: float, b: float, causal: bool) -> None:
    """A strictly closer match must never be tiered *below* a farther one,
    holding every other structured feature fixed — PLAN.md §5's property
    test requirement."""

    closer, farther = (a, b) if a <= b else (b, a)
    tier_closer = tier(_match(closer, causal=causal), _CANDIDATE).tier
    tier_farther = tier(_match(farther, causal=causal), _CANDIDATE).tier
    assert _RANK[tier_closer] >= _RANK[tier_farther]


@settings(max_examples=500)
@given(distance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False), causal=st.booleans())
def test_weak_and_none_are_never_in_blocking_tiers(distance: float, causal: bool) -> None:
    result = tier(_match(distance, causal=causal), _CANDIDATE)
    if result.tier in (MatchTier.WEAK, MatchTier.NONE):
        assert result.tier not in BLOCKING_TIERS
