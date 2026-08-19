"""Shared probe result type and sampling primitive — docs/project/PROJECT.md §4.4.

Every probe recomputes a bounded, deterministically-selected sample and
compares canonical bytes; none claims full equivalence, and the
``probe_runs`` table (db/decisions.py, D5) records ``sample_spec``,
``population_size``, and ``sample_size`` for exactly that reason — so the
UI can render "128/2400" next to the word "sample" rather than implying a
proof. Conservative mode always uses ``tolerance="bitwise"`` (docs/project/PROJECT.md
§3.2: ``atol = 0``); ``balanced`` mode is out of scope for D5/D6.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass

CONSERVATIVE_TOLERANCE = "bitwise"


@dataclass(frozen=True)
class ProbeResult:
    probe_type: str
    passed: bool
    sample_spec: str
    population_size: int
    sample_size: int
    tolerance: str
    runtime_ms: int
    evidence_digest: str
    detail: str = ""


class Timer:
    """`with Timer() as t: ...; t.ms` — a tiny stopwatch so every probe
    reports real wall-clock, not a guessed or hardcoded figure (docs/project/PLAN.md's
    anti-simulation lint bans literal timings in anything claiming to be
    measured)."""

    def __enter__(self) -> Timer:
        self._start = time.monotonic()
        self.ms = 0
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = int((time.monotonic() - self._start) * 1000)


def hash_select[T](salt: str, population: Sequence[T], k: int) -> list[T]:
    """Deterministically select up to `k` items from `population`.

    docs/project/PROJECT.md §4.4: "recompute k rows chosen by sha256(artifact_id ‖
    row_id) mod N". Implemented as a full ranking rather than a modulus
    filter so the selected count is always exactly ``min(k, len(population))``
    regardless of population size — a modulus filter's selected count
    varies with N, which would make "128/2400" not actually mean 128.
    """
    if k < 0:
        raise ValueError("k must be non-negative")
    ranked = sorted(
        population, key=lambda item: hashlib.sha256(f"{salt}:{item}".encode()).hexdigest()
    )
    return ranked[: min(k, len(ranked))]


def evidence_digest(*parts: object) -> str:
    """sha256 over a stable, sorted textual join — used for probe_runs'
    evidence_digest column. Not a content address of raw payload bytes
    (those are already covered by artifact_id); this is a digest of *what
    the probe actually compared*, for audit."""
    joined = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
