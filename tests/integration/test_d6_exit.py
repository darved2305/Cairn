"""D6 exit criteria — docs/project/PLAN.md D6: "a never-seen mpnet misconfiguration is
blocked at strong_semantic before any compute; applying the remediation
lets it through."

Uses the real sentence-transformer model (already vendored for the
`features` stage, docs/project/PROJECT.md §5.2) as the embedder here instead of
Bedrock Titan — Bedrock isn't reachable in this environment, and
docs/project/PLAN.md §13's own risk mitigation is explicit that "the demo's
correctness path never depends on the LLM" for exactly this reason. This
is not a mock: MiniLM is a real model producing real, semantically
meaningful embeddings; it just isn't the specific 1024-dim model the
production path uses. Cosine similarity is invariant to identically
zero-padding both vectors (dot product and both norms are unchanged), so
padding MiniLM's real 384-dim output to `failure_signatures.embedding`'s
required VECTOR(1024) preserves genuine semantic distance rather than
faking it.
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from cairn.db import memory
from cairn.db.memory import MatchTier, PlanFeatures
from cairn.workload import stage_features

pytestmark = pytest.mark.integration


def _semantic_embedding(text: str) -> list[float]:
    model = stage_features._get_model(stage_features.MAX_SEQ_LENGTH)  # noqa: SLF001
    vector = np.asarray(
        model.encode([text], normalize_embeddings=True, convert_to_numpy=True)[0], dtype=np.float32
    )
    padded = np.zeros(1024, dtype=np.float32)
    padded[: vector.shape[0]] = vector
    return padded.tolist()


def _f2_summary(*, input_dim: int) -> str:
    # Same template shape seed_memory.py's real seed_f2 uses — a genuine
    # summary_text for a specific misconfiguration, not hand-tuned prose.
    return (
        "checkpoint stage failed with ValueError: ValueError: training input "
        f"dimension is 768, configured input_dim is {input_dim} — the embedding "
        "model produces 768-d vectors (all-mpnet-base-v2) but "
        f"train.classifier.input_dim was still {input_dim}, from all-MiniLM-L6-v2."
    )


def test_never_seen_mpnet_misconfig_blocked_then_remediation_lets_it_through(
    pool, warm_embedding_model: None
) -> None:
    token = uuid.uuid4().hex
    error_class = f"ValueError-{token}"

    # --- seed the known failure + its verified remediation ---
    known_summary = _f2_summary(input_dim=384)
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="checkpoint",
            error_class=error_class,
            workload_kind="text-classification",
            traceback_head="ValueError: training input dimension is 768, configured input_dim is 384",
            summary_text=known_summary,
            embedding=_semantic_embedding(known_summary),
            run_id=uuid.uuid4(),
            wasted_ms=1200,
            embedding_dim=768,
            input_dim=384,
            model_family="sentence-transformers",
            framework_version="2.5.0",
        ),
    )
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[memory.ChangedKey(key="train.input_dim", from_value=384, to_value=768)],
            rationale="classifier.input_dim must equal the embedding model's actual output dimension",
            verified_run_id=uuid.uuid4(),
            succeeded=True,
        ),
    )

    # --- a NEW, never-seen run proposes the analogous mistake at a
    # different (also wrong) input_dim -> must be blocked BEFORE any
    # compute, i.e. before stage_train.run is ever called ---
    proposed_summary = _f2_summary(input_dim=512)
    proposed_embedding = _semantic_embedding(proposed_summary)
    candidate = PlanFeatures(
        stage="checkpoint",
        error_class_hint=error_class,
        # embedding_dim agrees with the recorded signature (the causal
        # key) but framework_version differs, so this is a near-miss —
        # strong_semantic, not an exact re-run of the identical config.
        structured={"embedding_dim": 768, "input_dim": 512, "framework_version": "2.9.0"},
    )

    matches = memory.search(
        pool, stage="checkpoint", error_class_hint=error_class, embedding=proposed_embedding
    )
    assert len(matches) == 1
    verdict = memory.tier(matches[0], candidate)
    assert verdict.tier == MatchTier.STRONG_SEMANTIC
    assert verdict.tier in memory.BLOCKING_TIERS, "must refuse the plan before launching compute"

    # --- applying the remediation (embedding_dim -> 768) and re-planning:
    # the causal key no longer agrees, so the SAME memory entry no longer
    # blocks — "applying the remediation lets it through" ---
    fixed_candidate = PlanFeatures(
        stage="checkpoint",
        error_class_hint=None,
        structured={"embedding_dim": 768, "input_dim": 768},
    )
    verdict_after_fix = memory.tier(matches[0], fixed_candidate)
    assert verdict_after_fix.tier not in memory.BLOCKING_TIERS
