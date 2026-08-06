"""P3 feature_resample against the real sentence-transformer model — no
mock stands in for the embedding call, matching PLAN.md's anti-simulation
rule. Marked integration (not unit) for the same reason test_determinism.py
is: it downloads and runs the real MiniLM model (network on first call,
then cached).

Batched transformer inference is not bit-exact across different batch
compositions (see p3_features.py's module docstring) — a real, disclosed
non-guarantee, not a bug. So the "recorded artifact" fixture here is built
using the exact same per-sample batching P3 itself uses internally
(`select_sample_doc_ids` then one `encode(sample_texts)` call), which
isolates what these tests are actually checking — real content drift
detection — from that orthogonal, already-documented batching effect.
"""

from __future__ import annotations

import numpy as np
import pytest

from cairn.probes import p3_features
from cairn.workload import stage_features

pytestmark = pytest.mark.integration

_TOPICS = [
    "The rocket launched into orbit carrying a new communications satellite payload.",
    "The mechanic replaced the timing belt and checked the brake pads carefully.",
    "The graphics card renders millions of triangles at a smooth frame rate.",
    "Diplomats met for hours to discuss the ceasefire terms and regional tensions.",
]


def _encode(texts: list[str]) -> np.ndarray:
    model = stage_features._get_model(stage_features.MAX_SEQ_LENGTH)  # noqa: SLF001
    return np.asarray(
        model.encode(texts, batch_size=32, show_progress_bar=False, convert_to_numpy=True),
        dtype=np.float32,
    )


def _recorded_embeddings_matching_p3s_own_batching(
    artifact_id: str, doc_ids: list[int], texts_by_doc_id: dict[int, str], *, sample_size: int
) -> np.ndarray:
    """Build a population-sized array where only the rows P3 will sample
    are populated (via the same single-batch `encode` call P3 uses
    internally) — everything else is untouched zeros, which is fine since
    P3 never reads a non-sampled row."""

    sample_ids = p3_features.select_sample_doc_ids(artifact_id, doc_ids, sample_size)
    sample_texts = [texts_by_doc_id[doc_id] for doc_id in sample_ids]
    sample_embeddings = _encode(sample_texts)

    population = np.zeros((len(doc_ids), sample_embeddings.shape[1]), dtype=np.float32)
    position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
    for i, doc_id in enumerate(sample_ids):
        population[position[doc_id]] = sample_embeddings[i]
    return population


def test_p3_passes_when_recomputation_matches_the_recorded_artifact(
    warm_embedding_model: None,
) -> None:
    doc_ids = list(range(40))
    texts_by_doc_id = {doc_id: _TOPICS[doc_id % 4] for doc_id in doc_ids}
    original_embeddings = _recorded_embeddings_matching_p3s_own_batching(
        "artifact-p3", doc_ids, texts_by_doc_id, sample_size=16
    )

    result = p3_features.run(
        "artifact-p3",
        doc_ids,
        original_embeddings,
        texts_by_doc_id,
        encode=_encode,
        sample_size=16,
    )
    assert result.passed
    assert result.sample_size == 16
    assert result.population_size == 40


def test_p3_fails_when_recorded_artifact_does_not_match_current_model_output(
    warm_embedding_model: None,
) -> None:
    doc_ids = list(range(40))
    texts_by_doc_id = {doc_id: _TOPICS[doc_id % 4] for doc_id in doc_ids}
    tampered = _recorded_embeddings_matching_p3s_own_batching(
        "artifact-p3-tampered", doc_ids, texts_by_doc_id, sample_size=16
    )
    tampered += 1.0  # a real content difference, not a batching artifact

    result = p3_features.run(
        "artifact-p3-tampered",
        doc_ids,
        tampered,
        texts_by_doc_id,
        encode=_encode,
        sample_size=16,
    )
    assert not result.passed
    assert "mismatched rows" in result.detail
