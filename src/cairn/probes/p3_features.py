"""P3 feature_resample — docs/project/PROJECT.md §4.4.

Proves: `sample_size` hash-selected rows are bit-identical between a
recorded features artifact and a fresh recomputation using the *current*
embedding model and code.

Does not prove: the remaining rows are — this is a sampling argument, and
the sample size/population are always reported alongside "passed" so the
UI never presents it as a proof of full-table equality. Combined with a
passing P1 (env identity) and a passing structural class, the residual
risk is that a change altered the computation for some rows but not for a
hash-selected `sample_size` of them, which — for the structural classes
Cairn admits — would require a dynamic-dispatch escape hatch that P2
already refuses on.

A second, model-specific non-guarantee worth stating plainly: batched
transformer inference is not strictly invariant to batch composition at
the bit level — padding a sequence alongside different neighbours can
shift floating-point rounding in attention/pooling by a few ULP, even
though the *content* is unchanged. Recomputing a `sample_size`-row sample
therefore compares against the recorded artifact under a *different*
batch shape than production used to produce it, so this probe can report
a spurious fail from batching alone, not just from a real semantic
change. Conservative mode's own rule absorbs this cleanly: "probe not
bit-exact -> recompute" (docs/project/PROJECT.md §3.2) — a false fail costs an
unnecessary recompute, never a wrongful reuse, which is the safe
direction. It does mean `original_embeddings` passed to `run()` should
ideally have been captured under the same per-sample batching this probe
itself uses (see `select_sample_doc_ids` + a single `encode(sample_texts)`
call) when the goal is to isolate *content* drift from *batching* drift —
which is exactly what a caller re-verifying a specific artifact's
provenance would do by re-deriving that reference once, at record time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from cairn.fingerprint.canon import canonical_float32_bytes
from cairn.probes.base import (
    CONSERVATIVE_TOLERANCE,
    ProbeResult,
    Timer,
    evidence_digest,
    hash_select,
)

PROBE_TYPE = "P3"
SAMPLE_SIZE = 64


def select_sample_doc_ids(
    artifact_id: str, doc_ids: Sequence[int], k: int = SAMPLE_SIZE
) -> list[int]:
    return sorted(hash_select(artifact_id, doc_ids, k))


def run(
    artifact_id: str,
    doc_ids: Sequence[int],
    original_embeddings: npt.NDArray[np.float32],
    texts_by_doc_id: Mapping[int, str],
    *,
    encode: Any,
    sample_size: int = SAMPLE_SIZE,
) -> ProbeResult:
    """`encode(texts: list[str]) -> npt.NDArray[np.float32]` is the live
    model call — passed in rather than imported here so this module has no
    hard dependency on sentence-transformers/torch being loadable just to
    import a probe, and so tests can exercise the real
    `workload.stage_features` model without this module reaching into it
    directly.
    """

    with Timer() as t:
        position = {doc_id: i for i, doc_id in enumerate(doc_ids)}
        sample_ids = select_sample_doc_ids(artifact_id, doc_ids, sample_size)
        sample_texts = [texts_by_doc_id[doc_id] for doc_id in sample_ids]
        recomputed = np.asarray(encode(sample_texts), dtype=np.float32)

        mismatches = []
        for i, doc_id in enumerate(sample_ids):
            original_row = original_embeddings[position[doc_id]]
            if canonical_float32_bytes(original_row) != canonical_float32_bytes(recomputed[i]):
                mismatches.append(doc_id)
        passed = not mismatches

    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec="sha256(artifact_id, row_id)-ranked top-k rows",
        population_size=len(doc_ids),
        sample_size=len(sample_ids),
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(artifact_id, sample_ids),
        detail="ok" if passed else f"mismatched rows: {mismatches}",
    )
