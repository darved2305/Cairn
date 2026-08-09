"""Flight Recorder typed vocabulary (Appendix B.1).

Frozen value types with invariants in ``__post_init__`` so an unrepresentable
state cannot reach CockroachDB. Digests are 64-character lowercase SHA-256 hex.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

Digest = str

_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_ABSENT: Final = "absent"


def _require_digest(value: str, *, field: str) -> None:
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be 64 lowercase hex chars, got {value!r}")


def _require_rel_path(path: str, *, field: str) -> None:
    if not path or path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"{field} must be workspace-relative, got {path!r}")
    if ".." in path.split("/") or ".." in path.split("\\"):
        raise ValueError(f"{field} must not contain '..' segments, got {path!r}")
    if len(path) >= 2 and path[1] == ":":
        raise ValueError(f"{field} must not contain a drive letter, got {path!r}")


class CoverageState(StrEnum):
    COMPLETE_SUPPORTED = "COMPLETE_SUPPORTED"
    COMPLETE_DECLARED = "COMPLETE_DECLARED"
    SHADOW_UNQUALIFIED = "SHADOW_UNQUALIFIED"
    INCOMPLETE_NETWORK = "INCOMPLETE_NETWORK"
    INCOMPLETE_TRACE_LOSS = "INCOMPLETE_TRACE_LOSS"
    INCOMPLETE_WRITE = "INCOMPLETE_WRITE"
    INCOMPLETE_PLATFORM = "INCOMPLETE_PLATFORM"
    INCOMPLETE_INPUT_RACE = "INCOMPLETE_INPUT_RACE"
    NONDETERMINISTIC = "NONDETERMINISTIC"

    @property
    def authorizes_reuse(self) -> bool:
        return self in (CoverageState.COMPLETE_SUPPORTED, CoverageState.COMPLETE_DECLARED)

    @property
    def is_incomplete(self) -> bool:
        return self.name.startswith("INCOMPLETE_")


class AccessMode(StrEnum):
    READ = "read"
    EXECUTE = "execute"
    ENUMERATE = "enumerate"
    NEGATIVE = "negative"
    WRITE = "write"

    @property
    def is_input(self) -> bool:
        return self is not AccessMode.WRITE


class Authority(StrEnum):
    """Deliberately has no MODEL member — models may advise, never authorize."""

    IDENTITY = "identity"
    STRUCTURAL = "structural"
    PROBE = "probe"


class PlanAction(StrEnum):
    RESTORE = "RESTORE"
    SUBSCRIBE = "SUBSCRIBE"
    TAKE_OVER = "TAKE_OVER"
    REPAIR = "REPAIR"
    RUN_LOCAL = "RUN_LOCAL"
    RUN_ECS = "RUN_ECS"
    RUN_SHADOW_LEARN = "RUN_SHADOW_LEARN"
    RUN_ISOLATED_QUALIFICATION = "RUN_ISOLATED_QUALIFICATION"
    REFUSE_REUSE = "REFUSE_REUSE"
    REPLAN_FAILURE = "REPLAN_FAILURE"


class ResourceKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SUBPROCESS = "subprocess"
    SOCKET = "socket"
    DEVICE = "device"
    ENV = "env"
    S3_OBJECT = "s3_object"


class ObservationSource(StrEnum):
    SYSCALL = "syscall"
    PYTHON = "python"
    ADAPTER = "adapter"
    DECLARED = "declared"


@dataclass(frozen=True, slots=True)
class OutputContract:
    path_rel: str
    kind: str = "regular_file"

    def __post_init__(self) -> None:
        if self.kind != "regular_file":
            raise ValueError(f"v0.1 supports only regular_file outputs, got {self.kind!r}")
        _require_rel_path(self.path_rel, field="output.path_rel")


@dataclass(frozen=True, slots=True)
class PlatformContract:
    os: str
    arch: str
    image_digest: str | None

    def __post_init__(self) -> None:
        if not self.os or not self.arch:
            raise ValueError("platform os/arch are required")
        if (
            self.image_digest is not None
            and not self.image_digest.startswith("sha256:")
            and not _DIGEST_RE.fullmatch(self.image_digest)
        ):
            raise ValueError(
                f"image_digest must be sha256:... or 64 hex chars, got {self.image_digest!r}"
            )


@dataclass(frozen=True, slots=True)
class PurityPolicy:
    contract_id: str
    network: str
    clock: str
    random: str
    env_names: tuple[str, ...]
    qualification_runs: int = 2

    def __post_init__(self) -> None:
        if self.qualification_runs < 1:
            raise ValueError("qualification_runs must be >= 1")
        if self.authorizes_reuse and self.qualification_runs < 2:
            raise ValueError("a purity policy that authorizes reuse requires qualification_runs >= 2")
        object.__setattr__(self, "env_names", tuple(sorted(self.env_names)))

    @property
    def authorizes_reuse(self) -> bool:
        return self.contract_id not in {"shadow", "SHADOW_UNQUALIFIED"}


@dataclass(frozen=True, slots=True)
class AdapterContract:
    adapter_id: str
    partitioner_digest: Digest
    reducer_digest: Digest
    verifier_digest: Digest
    partitions: int

    def __post_init__(self) -> None:
        _require_digest(self.partitioner_digest, field="partitioner_digest")
        _require_digest(self.reducer_digest, field="reducer_digest")
        _require_digest(self.verifier_digest, field="verifier_digest")
        if self.partitions < 1:
            raise ValueError("partitions must be >= 1")


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    kind: ResourceKind
    ref: str
    access_mode: AccessMode
    exists: bool
    version_digest: Digest
    resolver: str
    source: ObservationSource
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.access_mode is AccessMode.NEGATIVE:
            if self.exists:
                raise ValueError("NEGATIVE access requires exists=False")
            if self.version_digest != _ABSENT:
                raise ValueError("NEGATIVE access requires version_digest='absent'")
        else:
            if not self.exists:
                raise ValueError(f"{self.access_mode} access requires exists=True")
            _require_digest(self.version_digest, field="version_digest")
        if not self.ref:
            raise ValueError("resource ref must be non-empty")
        if not self.resolver:
            raise ValueError("resolver must be non-empty")

    @property
    def identity_tuple(self) -> tuple[str, str, str]:
        return (self.kind.value, self.ref, self.access_mode.value)

    def identity_payload(self) -> dict[str, object]:
        """Hashed projection — deliberately omits metadata."""
        return {
            "kind": self.kind.value,
            "ref": self.ref,
            "access_mode": self.access_mode.value,
            "exists": self.exists,
            "version_digest": self.version_digest,
            "resolver": self.resolver,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    namespace_id: str
    argv: tuple[str, ...]
    cwd_rel: str
    output: OutputContract
    platform: PlatformContract
    purity: PurityPolicy
    adapter: AdapterContract | None
    tracer_version: str
    companion_version: str
    adapter_registry_version: str

    def __post_init__(self) -> None:
        if not self.namespace_id:
            raise ValueError("namespace_id is required")
        if not self.argv:
            raise ValueError("argv must be a non-empty array, never a shell string")
        if any("\n" in part or "\x00" in part for part in self.argv):
            raise ValueError("argv elements must not contain newlines or NUL")
        _require_rel_path(self.cwd_rel, field="cwd_rel")
        if self.adapter is not None and self.purity.contract_id == "shadow":
            raise ValueError("an adapter may not run under the shadow contract")
        if not self.tracer_version or not self.companion_version:
            raise ValueError("tracer_version and companion_version are required")
        if not self.adapter_registry_version:
            raise ValueError("adapter_registry_version is required")


@dataclass(frozen=True, slots=True)
class TraceContent:
    coverage_state: CoverageState
    resources: tuple[ResourceIdentity, ...]
    incomplete_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        seen: set[tuple[str, str, str]] = set()
        for resource in self.resources:
            key = resource.identity_tuple
            if key in seen:
                raise ValueError(f"duplicate resource identity_tuple {key!r}")
            seen.add(key)
        sorted_resources = tuple(sorted(self.resources, key=lambda r: r.identity_tuple))
        object.__setattr__(self, "resources", sorted_resources)
        if self.coverage_state.authorizes_reuse and self.incomplete_reasons:
            raise ValueError("a coverage state that authorizes reuse may not carry incomplete reasons")
        if self.coverage_state.is_incomplete and not self.incomplete_reasons:
            raise ValueError("an INCOMPLETE_* state must state at least one reason")

    @property
    def inputs(self) -> tuple[ResourceIdentity, ...]:
        return tuple(r for r in self.resources if r.access_mode.is_input)

    @property
    def outputs(self) -> tuple[ResourceIdentity, ...]:
        return tuple(r for r in self.resources if r.access_mode is AccessMode.WRITE)
