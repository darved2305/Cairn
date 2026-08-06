"""Canonicalization primitives every fingerprint in Cairn is built on.

Two independent kinds of "same bytes every time" are needed across the
codebase: canonical JSON (for hashing structured config/manifests — the
work_key, the env fingerprint) and canonical float32 bytes (for
content-addressing and bitwise-comparing numeric arrays — the features
artifact, the P3/P4 probes in PROJECT.md §4.4). Both live here because
astcanon.py, env.py, and workkey.py all reduce to "hash canonical_json of
something," and features/checkpoint reduce to "hash canonical_float32_bytes
of something."
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import numpy.typing as npt


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, no incidental whitespace,
    stable across Python versions and across however the caller happened
    to build the dict. This is what work_key composition and env
    fingerprinting hash — see PROJECT.md §4.2's work_key definition."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def canonical_float32_bytes(arr: npt.ArrayLike) -> bytes:
    """Canonical little-endian float32 byte encoding, used to
    content-address embedding/logit arrays and to compare them bitwise.
    PROJECT.md P3/P4 probes are defined as "compare sha256 of the
    canonical little-endian float32 byte encoding" — this is that
    encoding. Forcing little-endian + C-contiguous means the same array
    hashes identically regardless of the producing machine's native byte
    order or whatever memory layout numpy happened to leave it in.
    """
    a = np.ascontiguousarray(arr, dtype="<f4")
    return a.tobytes()
