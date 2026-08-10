"""Flight preflight: structured failure memory may retrieve candidates only.

Similarity never authorizes. Exact structured conditions plus a verified
remediation history may refuse a doomed plan or propose remediation. Embedding
provider failures degrade to no memory match — never a cache hit.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from cairn.db import memory
from cairn.embeddings import EmbeddingError, default_provider


@dataclass(frozen=True, slots=True)
class PreflightOutcome:
    """Result of a memory preflight check against a proposed stage plan."""

    blocked: bool
    tier: str | None
    reason: str
    signature_id: str | None = None
    remediation_rationale: str | None = None


def preflight_structured_memory(
    pool: ConnectionPool,
    *,
    stage: str,
    error_class_hint: str,
    structured: dict[str, object],
    summary_text: str,
) -> PreflightOutcome:
    """Look up prior failures. On embedding/DB errors, fail open to run
    (unknown means run) without corrupting correctness.
    """
    try:
        embedding = default_provider().embed(summary_text)
    except EmbeddingError as exc:
        return PreflightOutcome(
            blocked=False,
            tier=None,
            reason=f"embedding unavailable; continuing without memory ({exc})",
        )

    try:
        matches = memory.search(
            pool,
            stage=stage,
            error_class_hint=error_class_hint,
            embedding=embedding,
        )
    except Exception as exc:  # noqa: BLE001 - preflight must not abort execution
        return PreflightOutcome(
            blocked=False,
            tier=None,
            reason=f"memory search unavailable; continuing without memory ({exc})",
        )

    candidate = memory.PlanFeatures(
        stage=stage, error_class_hint=error_class_hint, structured=structured
    )
    for match in matches:
        tiered = memory.tier(match, candidate)
        if tiered.tier not in memory.BLOCKING_TIERS:
            continue
        # Only exact structured + causal remediation authorizes a block.
        # strong_semantic is treated as candidate retrieval only while the
        # judged vector claim is cut (see eligibility/vector_claim_removed.json).
        if tiered.tier is not memory.MatchTier.EXACT:
            continue
        remediation = memory.latest_successful_remediation(pool, match.signature_id)
        rationale = remediation.rationale if remediation is not None else None
        return PreflightOutcome(
            blocked=True,
            tier=tiered.tier.value,
            reason=(
                "exact structured memory match refuses doomed config "
                "(authorized by verified remediation history, not similarity)"
            ),
            signature_id=str(match.signature_id),
            remediation_rationale=rationale,
        )
    return PreflightOutcome(
        blocked=False,
        tier=None,
        reason="no exact structured memory block",
    )
