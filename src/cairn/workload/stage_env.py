"""env stage: capture image digest, python version, resolved deps, and
torch thread config, then compute the env fingerprint. docs/project/PROJECT.md §5.2:
~2s. This is the first stage in the DAG (env -> dataset -> features ->
checkpoint -> eval) and every other artifact records this fingerprint as
one of its inputs.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
from dataclasses import dataclass

from cairn.fingerprint.env import env_fingerprint
from cairn.workload.determinism import NUM_THREADS


@dataclass(frozen=True)
class EnvManifest:
    env_fingerprint: str
    image_digest: str
    python_version: str
    deps: dict[str, str]
    torch_threads: int


def _resolved_deps() -> dict[str, str]:
    """{distribution_name: version} for everything installed, sorted by
    canonical_json's sort_keys — this is the "sorted pip freeze" docs/project/PROJECT.md
    calls for, read from interpreter metadata instead of shelling out to
    `pip freeze` (no subprocess, same information, faster)."""
    return {
        dist.metadata["Name"]: dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    }


def run() -> EnvManifest:
    # CAIRN_IMAGE_DIGEST is set by the container build (D7); "local-dev"
    # here is honest about the fact that a laptop run has no image to
    # digest, not a placeholder pretending to be one.
    image_digest = os.environ.get("CAIRN_IMAGE_DIGEST", "local-dev")
    python_version = platform.python_version()
    deps = _resolved_deps()

    fingerprint = env_fingerprint(
        image_digest=image_digest,
        python_version=python_version,
        deps=deps,
        torch_threads=NUM_THREADS,
    )
    return EnvManifest(
        env_fingerprint=fingerprint,
        image_digest=image_digest,
        python_version=python_version,
        deps=deps,
        torch_threads=NUM_THREADS,
    )
