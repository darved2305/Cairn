"""Deterministic composition of Cairn work keys."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from cairn.fingerprint.astcanon import CodeUnit
from cairn.fingerprint.canon import canonical_json

WORK_KEY_VERSION = 1


@dataclass(frozen=True)
class WorkKey:
    value: str
    stage: str
    code_fingerprint: str
    data_fingerprint: str
    config_fingerprint: str
    env_fingerprint: str
    upstream: tuple[str, ...]


def code_fingerprint(units: Iterable[CodeUnit]) -> str:
    leaves = sorted((unit.unit_id, unit.ast_digest) for unit in units)
    return _digest({"v": WORK_KEY_VERSION, "units": leaves})


def config_fingerprint(reads: Mapping[str, object]) -> str:
    return _digest({"v": WORK_KEY_VERSION, "reads": dict(reads)})


def make_work_key(
    *,
    stage: str,
    code_fingerprint: str,
    data_fingerprint: str,
    config_fingerprint: str,
    env_fingerprint: str,
    upstream: Iterable[str] = (),
) -> WorkKey:
    if not stage.strip():
        raise ValueError("stage must be non-empty")
    sorted_upstream = tuple(sorted(upstream))
    payload = {
        "v": WORK_KEY_VERSION,
        "stage": stage,
        "code_fingerprint": code_fingerprint,
        "data_fingerprint": data_fingerprint,
        "config_fingerprint": config_fingerprint,
        "env_fingerprint": env_fingerprint,
        "upstream": sorted_upstream,
    }
    return WorkKey(
        value=_digest(payload),
        stage=stage,
        code_fingerprint=code_fingerprint,
        data_fingerprint=data_fingerprint,
        config_fingerprint=config_fingerprint,
        env_fingerprint=env_fingerprint,
        upstream=sorted_upstream,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
