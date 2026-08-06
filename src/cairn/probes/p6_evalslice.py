"""P6 eval_slice_replay — PROJECT.md §4.4.

Proves: the evaluation code produces the same number on the same
deterministic 200-example slice, recomputed bitwise (conservative mode).

Does not prove: the full-set metric is unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score

from cairn.probes.base import (
    CONSERVATIVE_TOLERANCE,
    ProbeResult,
    Timer,
    evidence_digest,
    hash_select,
)
from cairn.workload.stage_eval import SUPPORTED_METRICS
from cairn.workload.stage_train import Classifier

PROBE_TYPE = "P6"
SAMPLE_SIZE = 200


def select_sample_doc_ids(
    salt: str, test_doc_ids: Sequence[int], k: int = SAMPLE_SIZE
) -> list[int]:
    return sorted(hash_select(f"eval_slice:{salt}", test_doc_ids, k))


def _compute_metrics(
    embeddings: np.ndarray,
    labels: np.ndarray,
    state_dict_bytes: bytes,
    *,
    input_dim: int,
    hidden_dim: int,
    num_labels: int,
) -> dict[str, float]:
    from io import BytesIO

    model = Classifier(input_dim=input_dim, hidden_dim=hidden_dim, num_labels=num_labels)
    model.load_state_dict(torch.load(BytesIO(state_dict_bytes), weights_only=True))
    model.eval()
    with torch.inference_mode():
        logits = model(torch.from_numpy(embeddings))
        preds = logits.argmax(dim=1).numpy()
    return {
        "accuracy": float(accuracy_score(labels, preds)),
        "macro_f1": float(f1_score(labels, preds, average="macro", zero_division=0)),
    }


def run(
    artifact_id: str,
    test_doc_ids: Sequence[int],
    embeddings: np.ndarray,
    labels: np.ndarray,
    old_state_dict_bytes: bytes,
    new_state_dict_bytes: bytes,
    *,
    metrics: Sequence[str],
    input_dim: int,
    hidden_dim: int,
    num_labels: int,
    sample_size: int = SAMPLE_SIZE,
) -> ProbeResult:
    unknown = sorted(set(metrics) - SUPPORTED_METRICS)
    if unknown:
        raise ValueError(f"unsupported eval metrics: {', '.join(unknown)}")

    with Timer() as t:
        position = {doc_id: i for i, doc_id in enumerate(test_doc_ids)}
        sample_ids = select_sample_doc_ids(artifact_id, test_doc_ids, sample_size)
        rows = [position[doc_id] for doc_id in sample_ids]
        x = embeddings[rows]
        y = labels[rows]

        old_metrics = _compute_metrics(
            x,
            y,
            old_state_dict_bytes,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            num_labels=num_labels,
        )
        new_metrics = _compute_metrics(
            x,
            y,
            new_state_dict_bytes,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            num_labels=num_labels,
        )
        mismatched = [m for m in metrics if old_metrics[m] != new_metrics[m]]
        passed = not mismatched

    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec="sha256(artifact_id, row_id)-ranked top-k test-split rows",
        population_size=len(test_doc_ids),
        sample_size=len(sample_ids),
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(artifact_id, sample_ids, sorted(metrics)),
        detail="ok" if passed else f"metrics differ on the slice: {mismatched}",
    )
