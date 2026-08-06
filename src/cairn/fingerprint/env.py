"""Environment fingerprint: hash of {base image digest, python version,
sorted pip freeze, torch thread config} — PROJECT.md §4.2's work_key
definition. Conservative mode never trusts a probe across environments
(PROJECT.md §3.2), so this is the one input to work_key that, if it
differs at all, forces recompute regardless of anything else."""

from __future__ import annotations

import hashlib

from cairn.fingerprint.canon import canonical_json


def env_fingerprint(
    *, image_digest: str, python_version: str, deps: dict[str, str], torch_threads: int
) -> str:
    payload = {
        "image_digest": image_digest,
        "python_version": python_version,
        "deps": deps,
        "torch_threads": torch_threads,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()
