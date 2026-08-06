"""Determinism pins for the workload stages. PROJECT.md §5.2: "same image +
same inputs => bit-identical outputs, asserted over 3 runs." Call apply()
once, first thing, before running any stage.

Two gotchas worth being explicit about, because both fail silently if you
get them wrong:

1. `PYTHONHASHSEED` is read once by the interpreter at process startup,
   before any user code runs. Setting it from inside a running process —
   `os.environ["PYTHONHASHSEED"] = "0"` — does nothing; hash randomization
   is already locked in for this process. apply() checks it's already
   correct and fails loudly if not, rather than pretending to fix
   something it can't.
2. Thread-count env vars (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`) are only
   reliable if set before the BLAS backend spins up its thread pool, which
   can happen at import time depending on the backend. `torch.set_num_threads`
   is the documented, always-reliable way to pin torch's intra-op
   parallelism regardless of import order, so apply() uses that as the
   authority and treats the env vars as a best-effort backstop for any
   non-torch BLAS use.
"""

from __future__ import annotations

import os
import random
import sys

SEED = 1337
NUM_THREADS = 1

# PROJECT.md §5.3 F3: bounded so an over-large allocation raises a
# catchable Python-level MemoryError instead of the kernel OOM-killing the
# whole container. POSIX-only (see _set_memory_ceiling).
MEMORY_CEILING_BYTES = 3 * 1024**3


def apply() -> None:
    _check_hash_seed()
    os.environ.setdefault("OMP_NUM_THREADS", str(NUM_THREADS))
    os.environ.setdefault("MKL_NUM_THREADS", str(NUM_THREADS))
    random.seed(SEED)
    _seed_numpy()
    _seed_torch()
    _set_memory_ceiling()


def _check_hash_seed() -> None:
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuntimeError(
            "PYTHONHASHSEED=0 must be set in the process environment *before* "
            "the interpreter starts — e.g. `PYTHONHASHSEED=0 python -m cairn ...` "
            "or the container image's ENV. Setting it after the process has "
            "already started has no effect, so this fails loudly instead of "
            "silently running a pipeline whose determinism guarantee is false."
        )


def _seed_numpy() -> None:
    import numpy as np

    np.random.seed(SEED)


def _seed_torch() -> None:
    import torch

    torch.manual_seed(SEED)
    torch.set_num_threads(NUM_THREADS)
    torch.use_deterministic_algorithms(True)


def _set_memory_ceiling() -> None:
    if sys.platform == "win32":
        # RLIMIT_AS doesn't exist on Windows. Harmless for local dev; the
        # real enforcement point is the Linux container this actually ships
        # in (ECS Fargate), and F3's demo runs there, not on a laptop.
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_CEILING_BYTES, MEMORY_CEILING_BYTES))
