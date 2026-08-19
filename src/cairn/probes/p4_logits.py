"""P4 checkpoint_logit — docs/project/PROJECT.md §4.4.

Proves: two checkpoints produce bitwise-identical logits on a fixed,
deterministically-selected batch (`model.eval()`, `torch.inference_mode()`).
Conservative mode: `atol = 0`, justified only because P1 (env identity)
passed and workload/determinism.py pins the process deterministic.

Does not prove: identical outputs on all inputs, and does not detect a
change that alters behaviour only outside the probe batch.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

import numpy as np
import numpy.typing as npt
import torch

from cairn.fingerprint.canon import canonical_float32_bytes
from cairn.probes.base import (
    CONSERVATIVE_TOLERANCE,
    ProbeResult,
    Timer,
    evidence_digest,
    hash_select,
)
from cairn.workload.stage_train import Classifier

PROBE_TYPE = "P4"
SAMPLE_SIZE = 128


def select_sample_doc_ids(
    artifact_id: str, doc_ids: Sequence[int], k: int = SAMPLE_SIZE
) -> list[int]:
    return sorted(hash_select(artifact_id, doc_ids, k))


def _logits(
    state_dict_bytes: bytes, x: torch.Tensor, *, input_dim: int, hidden_dim: int, num_labels: int
) -> npt.NDArray[np.float32]:
    model = Classifier(input_dim=input_dim, hidden_dim=hidden_dim, num_labels=num_labels)
    # weights_only=True: a checkpoint is untrusted input as far as pickle is
    # concerned — mirrors stage_eval.run's own load.
    model.load_state_dict(torch.load(BytesIO(state_dict_bytes), weights_only=True))
    model.eval()
    with torch.inference_mode():
        out: npt.NDArray[np.float32] = model(x).numpy()
    return out


def run(
    artifact_id: str,
    doc_ids: Sequence[int],
    embeddings: npt.NDArray[np.float32],
    old_state_dict_bytes: bytes,
    new_state_dict_bytes: bytes,
    *,
    input_dim: int,
    hidden_dim: int,
    num_labels: int,
    sample_size: int = SAMPLE_SIZE,
) -> ProbeResult:
    with Timer() as t:
        position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
        sample_ids = select_sample_doc_ids(artifact_id, doc_ids, sample_size)
        rows = [position[doc_id] for doc_id in sample_ids]
        x = torch.from_numpy(embeddings[rows])

        old_logits = _logits(
            old_state_dict_bytes,
            x,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            num_labels=num_labels,
        )
        new_logits = _logits(
            new_state_dict_bytes,
            x,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            num_labels=num_labels,
        )
        passed = canonical_float32_bytes(old_logits) == canonical_float32_bytes(new_logits)

    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec="sha256(artifact_id, row_id)-ranked top-k rows, fixed batch",
        population_size=len(doc_ids),
        sample_size=len(sample_ids),
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(artifact_id, sample_ids),
        detail="ok" if passed else "logits differ on the probe batch",
    )
