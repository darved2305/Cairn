"""Configuration with causal read tracking.

Stages read through :class:`StageConfig` instead of indexing a raw mapping.
Every successful lookup records the fully-qualified key and the exact value
that was observed. Work-key composition hashes only that snapshot, so a
change to ``eval.metrics`` cannot invalidate the features or checkpoint
stages merely because all settings happen to live in one file.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from cairn.fingerprint.canon import canonical_json

_MISSING: Final = object()


class ConfigError(ValueError):
    """The config is malformed or a requested key has an invalid value."""


class TrackedConfig:
    """Immutable-by-convention config data plus per-stage read snapshots."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = copy.deepcopy(dict(data))
        _validate_mapping(self._data, path="<root>")
        self._reads: dict[str, dict[str, Any]] = {}

    @classmethod
    def load(cls, path: str | Path) -> TrackedConfig:
        source = Path(path)
        try:
            parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"could not load config {source}: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ConfigError(f"config {source} must contain a mapping at its root")
        return cls(parsed)

    def stage(self, name: str, *, record_as: str | None = None) -> StageConfig:
        if not name or "." in name:
            raise ConfigError("stage name must be a non-empty top-level key")
        return StageConfig(self, name, record_as=record_as)

    def get(
        self,
        path: str,
        *,
        stage: str,
        default: Any = _MISSING,
        expected_type: type[Any] | tuple[type[Any], ...] | None = None,
    ) -> Any:
        """Read and record one fully-qualified dotted path.

        Defaults are recorded too: the default became a causal input when the
        stage used it. Returned mutable values are copied so callers cannot
        silently mutate the source config after it has been fingerprinted.
        """

        if not stage:
            raise ConfigError("a stage name is required for tracked reads")
        value = self._lookup(path, default)
        if expected_type is not None and not isinstance(value, expected_type):
            expected = _type_name(expected_type)
            raise ConfigError(f"config key {path!r} must be {expected}, got {type(value).__name__}")
        copied = copy.deepcopy(value)
        self._reads.setdefault(stage, {})[path] = copied
        return copy.deepcopy(copied)

    def reads(self, stage: str) -> dict[str, Any]:
        return copy.deepcopy(self._reads.get(stage, {}))

    def reset_reads(self, stage: str | None = None) -> None:
        if stage is None:
            self._reads.clear()
        else:
            self._reads.pop(stage, None)

    def read_fingerprint(self, stage: str) -> str:
        import hashlib

        return hashlib.sha256(canonical_json(self.reads(stage))).hexdigest()

    def _lookup(self, path: str, default: Any) -> Any:
        parts = path.split(".")
        if not path or any(not part for part in parts):
            raise ConfigError(f"invalid dotted config path {path!r}")
        current: Any = self._data
        for part in parts:
            if not isinstance(current, Mapping) or part not in current:
                if default is not _MISSING:
                    return default
                raise ConfigError(f"required config key {path!r} is missing")
            current = current[part]
        return current


class StageConfig:
    """A stage-scoped facade that records keys as ``<stage>.<key>``."""

    def __init__(
        self, config: TrackedConfig, section: str, *, record_as: str | None = None
    ) -> None:
        self._config = config
        self.section = section
        self.stage = record_as or section

    def get(
        self,
        key: str,
        *,
        default: Any = _MISSING,
        expected_type: type[Any] | tuple[type[Any], ...] | None = None,
    ) -> Any:
        if not key or key.startswith(".") or key.endswith("."):
            raise ConfigError(f"invalid stage-relative config key {key!r}")
        return self._config.get(
            f"{self.section}.{key}",
            stage=self.stage,
            default=default,
            expected_type=expected_type,
        )

    def reads(self) -> dict[str, Any]:
        return self._config.reads(self.stage)


def _validate_mapping(value: Mapping[str, Any], *, path: str) -> None:
    for key, child in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"config mapping {path} contains a non-string or empty key")
        if "." in key:
            raise ConfigError(f"config key {key!r} at {path} cannot contain '.'")
        if isinstance(child, Mapping):
            _validate_mapping(child, path=f"{path}.{key}")


def _type_name(expected: type[Any] | tuple[type[Any], ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__
