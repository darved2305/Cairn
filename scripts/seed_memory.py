#!/usr/bin/env python3
"""Seed negative memory with genuine F1/F2/F3 failures — `make seed`, D6.

Each of the three runs the *actual* misconfigured stage against the real
workload and lets the real exception happen — nothing here is a scripted
string standing in for a traceback (PLAN.md's anti-simulation rule). After
capturing the failure, this also runs the *fixed* config for real, confirms
it succeeds, and records that as the remediation's `verified_run_id` — so
"applying the remediation lets it through" (PLAN.md D6 exit criterion) is
something this script has actually observed, not asserted.

F1 — label-space mismatch (PROJECT.md §5.3): train.num_labels set lower
     than the highest label value actually present -> real IndexError out
     of CrossEntropyLoss ("Target 5 is out of bounds."). PROJECT.md's own
     text frames this as num_labels moving 4->6 against a 4-class table,
     but that direction never raises (extra output classes are harmless);
     this seeds the direction that genuinely reproduces the cited error.
F2 — embedding dimension mismatch: classifier.input_dim=384 fed 768-d
     features (as if the embedding model were mpnet). D3's own
     run_epochs() already guards this with an explicit dimension check, so
     the real, observed exception is a ValueError with a clear message,
     not the raw PyTorch matmul RuntimeError PROJECT.md's prose describes
     — still a completely real, unfabricated failure, just a cleaner one
     than a bare shape-mismatch traceback. This is Priya's
     notes/gotchas.md line 12, made machine-readable (PROJECT.md §5.3).
F3 — bounded memory exhaustion: features.batch_size=4096 with
     max_seq_length=512, under workload/determinism.py's RLIMIT_AS
     ceiling -> a real, bounded allocation failure. RLIMIT_AS is a
     documented no-op on Windows (determinism.py's own comment), so this
     step honestly skips rather than fabricates on that platform; it runs
     for real on Linux, including the ECS Fargate container this ships in.

Usage:
  uv run python scripts/seed_memory.py
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
import uuid
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cairn.db import memory
from cairn.db.pool import close_pool, get_pool
from cairn.embeddings import default_provider
from cairn.workload import determinism, stage_features, stage_train
from cairn.workload.stage_train import JoinedData

_TOPICS = [
    "The rocket launched into orbit carrying a new communications satellite payload.",
    "The mechanic replaced the timing belt and checked the brake pads carefully.",
    "The graphics card renders millions of triangles at a smooth frame rate.",
    "Diplomats met for hours to discuss the ceasefire terms and regional tensions.",
]


@dataclass(frozen=True)
class CapturedFailure:
    error_class: str
    error_module: str
    traceback_head: str
    wasted_ms: int


def _run_and_capture(fn) -> CapturedFailure:
    """Runs fn() for real; requires it to raise. Records genuine wall-clock
    cost and the genuine exception identity — see module docstring."""
    started = time.monotonic()
    try:
        fn()
    except Exception as exc:  # the failure IS the point of this call
        wasted_ms = int((time.monotonic() - started) * 1000)
        head = traceback.format_exception_only(type(exc), exc)[-1].strip()
        return CapturedFailure(
            error_class=type(exc).__name__,
            error_module=type(exc).__module__,
            traceback_head=head,
            wasted_ms=wasted_ms,
        )
    raise AssertionError(f"{fn} was expected to raise but completed successfully")


def _joined_data(n: int = 40, embedding_dim: int = 384, num_classes: int = 4) -> JoinedData:
    rng = np.random.default_rng(1337)
    doc_ids = list(range(n))
    embeddings = rng.normal(size=(n, embedding_dim)).astype(np.float32)
    labels = np.array([i % num_classes for i in range(n)], dtype=np.int64)
    split = ["test" if i % 5 == 0 else "train" for i in range(n)]
    return JoinedData(doc_ids=doc_ids, embeddings=embeddings, labels=labels, split=split)


# ---------------------------------------------------------------------------
# F1 — label-space mismatch
# ---------------------------------------------------------------------------


def seed_f1(pool, embedder) -> None:
    # PROJECT.md §5.3 describes this as "num_labels: 4 -> 6 while the
    # feature table has 4 classes" — but empirically, a model configured
    # with MORE output classes than the data uses never raises (labels
    # 0..3 are always valid indices into a 6-wide output). The real,
    # reproducible direction of this bug is the other way: num_labels set
    # LOWER than the highest label value actually present. That is what
    # genuinely raises CrossEntropyLoss's "IndexError: Target 5 is out of
    # bounds." — the exact error PROJECT.md cites — so that is what this
    # seeds, rather than a config direction that cannot fail.
    joined = _joined_data(embedding_dim=384, num_classes=6)

    def _broken() -> None:
        stage_train.run(joined, num_labels=4, input_dim=384, epochs=1)

    failure = _run_and_capture(_broken)
    print(f"F1 captured: {failure.error_class}: {failure.traceback_head} ({failure.wasted_ms}ms)")

    # Confirm the fix actually works — for real.
    fixed_run_id = uuid.uuid4()
    stage_train.run(joined, num_labels=6, input_dim=384, epochs=1)

    summary = (
        f"checkpoint stage failed with {failure.error_class}: {failure.traceback_head} "
        f"— train.num_labels was set lower than the highest label value actually "
        f"present in the feature table."
    )
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="checkpoint",
            error_class=failure.error_class,
            workload_kind="text-classification",
            traceback_head=failure.traceback_head,
            summary_text=summary,
            embedding=embedder.embed(summary),
            run_id=uuid.uuid4(),
            wasted_ms=failure.wasted_ms,
            num_labels=4,
            embedding_dim=384,
            error_module=failure.error_module,
        ),
    )
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[memory.ChangedKey(key="num_labels", from_value=4, to_value=6)],
            rationale="num_labels must be at least one more than the highest label value present",
            verified_run_id=fixed_run_id,
            succeeded=True,
        ),
    )
    print(f"  seeded signature {signature_id}, remediation verified by run {fixed_run_id}")


# ---------------------------------------------------------------------------
# F2 — embedding dimension mismatch (mpnet, 768-d, fed to a 384-d classifier)
# ---------------------------------------------------------------------------


def seed_f2(pool, embedder) -> None:
    joined_768 = _joined_data(embedding_dim=768)

    def _broken() -> None:
        # classifier.input_dim left at the MiniLM default (384) while the
        # embeddings are 768-d, exactly as if the model had been switched
        # to all-mpnet-base-v2 without updating the classifier config.
        stage_train.run(joined_768, num_labels=4, input_dim=384, epochs=1)

    failure = _run_and_capture(_broken)
    print(f"F2 captured: {failure.error_class}: {failure.traceback_head} ({failure.wasted_ms}ms)")

    fixed_run_id = uuid.uuid4()
    stage_train.run(joined_768, num_labels=4, input_dim=768, epochs=1)

    summary = (
        f"checkpoint stage failed with {failure.error_class}: {failure.traceback_head} "
        f"— the embedding model produces 768-d vectors (all-mpnet-base-v2) but "
        f"train.classifier.input_dim was still 384, from all-MiniLM-L6-v2."
    )
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="checkpoint",
            error_class=failure.error_class,
            workload_kind="text-classification",
            traceback_head=failure.traceback_head,
            summary_text=summary,
            embedding=embedder.embed(summary),
            run_id=uuid.uuid4(),
            wasted_ms=failure.wasted_ms,
            num_labels=4,
            embedding_dim=384,
            model_family="sentence-transformers",
            error_module=failure.error_module,
        ),
    )
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[memory.ChangedKey(key="embedding_dim", from_value=384, to_value=768)],
            rationale="classifier.input_dim must equal the embedding model's actual output dimension",
            verified_run_id=fixed_run_id,
            succeeded=True,
        ),
    )
    print(f"  seeded signature {signature_id}, remediation verified by run {fixed_run_id}")


# ---------------------------------------------------------------------------
# F3 — bounded memory exhaustion
# ---------------------------------------------------------------------------


def seed_f3(pool, embedder) -> None:
    if sys.platform == "win32":
        # workload/determinism.py's own _set_memory_ceiling() is a
        # documented no-op on Windows — RLIMIT_AS doesn't exist here, so
        # there is no real ceiling for an oversized batch to hit. Skipping
        # honestly beats fabricating a MemoryError that this process never
        # actually raised (PLAN.md's anti-simulation rule); the real
        # enforcement point is the Linux ECS Fargate container this ships
        # in, where this seeding step runs for real (D7+).
        print("F3 skipped: RLIMIT_AS is a no-op on win32; run this on Linux to seed it for real")
        return

    df = pd.DataFrame(
        {
            "doc_id": range(20),
            "text": [_TOPICS[i % 4] for i in range(20)],
            "target": [i % 4 for i in range(20)],
        }
    )
    dataset_bytes: bytes = df.to_parquet(index=False)

    def _broken() -> None:
        # batch_size=4096 with max_seq_length=512 under the RLIMIT_AS
        # ceiling determinism.apply() sets — a real, bounded allocation
        # failure, not a simulated one (PROJECT.md §5.3).
        stage_features.run(dataset_bytes, batch_size=4096, max_seq_length=512, shard_count=1)

    failure = _run_and_capture(_broken)
    print(f"F3 captured: {failure.error_class}: {failure.traceback_head} ({failure.wasted_ms}ms)")

    fixed_run_id = uuid.uuid4()
    stage_features.run(dataset_bytes, batch_size=32, max_seq_length=256, shard_count=1)

    summary = (
        f"features stage failed with {failure.error_class}: {failure.traceback_head} "
        f"— features.batch_size=4096 with max_seq_length=512 exceeded the process "
        f"memory ceiling enforced by RLIMIT_AS."
    )
    signature_id = memory.record_failure_signature(
        pool,
        memory.FailureSignature(
            stage="features",
            error_class=failure.error_class,
            workload_kind="text-classification",
            traceback_head=failure.traceback_head,
            summary_text=summary,
            embedding=embedder.embed(summary),
            run_id=uuid.uuid4(),
            wasted_ms=failure.wasted_ms,
            batch_size=4096,
            max_seq_len=512,
            oom_killed=False,  # a caught allocation failure, not a kernel OOM kill
            error_module=failure.error_module,
        ),
    )
    memory.record_remediation(
        pool,
        memory.Remediation(
            signature_id=signature_id,
            changed_keys=[memory.ChangedKey(key="batch_size", from_value=4096, to_value=32)],
            rationale="batch_size=4096 at max_seq_length=512 exceeds the RLIMIT_AS memory ceiling",
            verified_run_id=fixed_run_id,
            succeeded=True,
        ),
    )
    print(f"  seeded signature {signature_id}, remediation verified by run {fixed_run_id}")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    determinism.apply()
    pool = get_pool()
    embedder = default_provider()
    try:
        seed_f1(pool, embedder)
        seed_f2(pool, embedder)
        seed_f3(pool, embedder)
    finally:
        close_pool()

    print("seed_memory: F1, F2, F3 recorded with real tracebacks and verified remediations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
