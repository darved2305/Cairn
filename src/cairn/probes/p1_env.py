"""P1 env_identity — PROJECT.md §4.4.

Proves: base image digest, Python version, and the full resolved
dependency set are identical between the artifact's recorded environment
and the current one (fingerprint/env.py's env_fingerprint already reduces
all of that to one hash).

Does not prove: identical CPU microarchitecture, identical BLAS kernel
selection, or absence of nondeterminism from thread scheduling.
workload/determinism.py pins `OMP_NUM_THREADS=1`,
`torch.use_deterministic_algorithms(True)`, and `PYTHONHASHSEED=0` so that
gap is closed *for the workload Cairn runs* — not in general, and nothing
here claims otherwise.
"""

from __future__ import annotations

from cairn.probes.base import CONSERVATIVE_TOLERANCE, ProbeResult, Timer, evidence_digest

PROBE_TYPE = "P1"


def run(recorded_env_fingerprint: str, current_env_fingerprint: str) -> ProbeResult:
    with Timer() as t:
        passed = recorded_env_fingerprint == current_env_fingerprint
    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec="env_fingerprint equality (whole environment, not sampled)",
        population_size=1,
        sample_size=1,
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(recorded_env_fingerprint, current_env_fingerprint),
        detail="ok"
        if passed
        else "env_fingerprint differs; conservative mode never trusts a probe across environments",
    )
