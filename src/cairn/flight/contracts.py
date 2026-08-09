"""Named purity contracts for ``cairn exec`` (Day 3 progressive trust).

``shadow`` — plan and compare, never reuse.
``deterministic-file/v1`` — user-asserted purity; coverage is
``COMPLETE_DECLARED`` (conspicuous), not a claim that tracing proved
determinism.
``jsonl-map/v1`` — bundled adapter contract; may coalesce first live work
but must not future-reuse a mere candidate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cairn.flight.types import (
    AdapterContract,
    CoverageState,
    Digest,
    ExecutionSpec,
    OutputContract,
    PlatformContract,
    PurityPolicy,
)
from cairn.trace.collector import TRACER_VERSION
from cairn.trace.companion import COMPANION_VERSION
from cairn.trace.scout import ADAPTER_REGISTRY_VERSION, ARBITRARY_EXEC_COVERAGE

CONTRACT_SHADOW = "shadow"
CONTRACT_DETERMINISTIC_FILE = "deterministic-file/v1"
CONTRACT_JSONL_MAP = "jsonl-map/v1"

KNOWN_CONTRACTS = frozenset(
    {CONTRACT_SHADOW, CONTRACT_DETERMINISTIC_FILE, CONTRACT_JSONL_MAP}
)


def _sha256_file(path: Path) -> Digest:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_oci_image_ref(oci_image: str | None) -> tuple[str | None, str | None]:
    """Return ``(full_ref, image_digest)`` from ``repo@sha256:HEX``.

    Tags without digests are refused for verified/ECS paths by the caller.
    """
    if oci_image is None or not oci_image.strip():
        return None, None
    ref = oci_image.strip()
    if "@sha256:" not in ref:
        return ref, None
    digest = "sha256:" + ref.rsplit("@sha256:", 1)[1]
    return ref, digest


def build_execution_spec(
    *,
    contract_id: str,
    argv: tuple[str, ...],
    cwd_rel: str,
    output_rel: str,
    namespace_id: str,
    platform: PlatformContract,
    env_names: tuple[str, ...] = (),
    network: str = "deny",
    adapter: AdapterContract | None = None,
) -> ExecutionSpec:
    if contract_id not in KNOWN_CONTRACTS:
        raise ValueError(f"unknown contract {contract_id!r}; known={sorted(KNOWN_CONTRACTS)}")
    if contract_id == CONTRACT_SHADOW:
        purity = PurityPolicy(
            contract_id=CONTRACT_SHADOW,
            network="observe",
            clock="observe",
            random="observe",
            env_names=env_names,
            qualification_runs=1,
        )
        if adapter is not None:
            raise ValueError("an adapter may not run under the shadow contract")
    elif contract_id == CONTRACT_DETERMINISTIC_FILE:
        purity = PurityPolicy(
            contract_id=CONTRACT_DETERMINISTIC_FILE,
            network=network,
            clock="deny",
            random="deny",
            env_names=env_names,
            qualification_runs=2,
        )
        if adapter is not None:
            raise ValueError("deterministic-file/v1 does not take an adapter")
    else:
        if adapter is None:
            raise ValueError("jsonl-map/v1 requires an AdapterContract")
        purity = PurityPolicy(
            contract_id=CONTRACT_JSONL_MAP,
            network=network,
            clock="deny",
            random="deny",
            env_names=env_names,
            qualification_runs=2,
        )
    return ExecutionSpec(
        namespace_id=namespace_id,
        argv=argv,
        cwd_rel=cwd_rel,
        output=OutputContract(path_rel=output_rel),
        platform=platform,
        purity=purity,
        adapter=adapter,
        tracer_version=TRACER_VERSION,
        companion_version=COMPANION_VERSION,
        adapter_registry_version=ADAPTER_REGISTRY_VERSION,
    )


def declared_coverage_for_contract(contract_id: str, observed: CoverageState) -> CoverageState:
    """Map post-run coverage under a named contract.

    Arbitrary opaque exec stays at Gate A's ``SHADOW_UNQUALIFIED``.
    ``deterministic-file/v1`` asserts purity conspicuously as
    ``COMPLETE_DECLARED`` unless the collector already forced incomplete/
    nondeterministic/network.
    """
    if contract_id == CONTRACT_SHADOW:
        # Honour Gate A freeze for arbitrary opaque exec.
        if observed.authorizes_reuse:
            return ARBITRARY_EXEC_COVERAGE
        return (
            observed
            if observed is not CoverageState.COMPLETE_SUPPORTED
            else ARBITRARY_EXEC_COVERAGE
        )
    if observed.is_incomplete or observed is CoverageState.NONDETERMINISTIC:
        return observed
    if contract_id in {CONTRACT_DETERMINISTIC_FILE, CONTRACT_JSONL_MAP}:
        return CoverageState.COMPLETE_DECLARED
    return observed


def _adapter_module_path() -> Path:
    import cairn.adapters.jsonl_map as _mod

    return Path(_mod.__file__)


def jsonl_map_adapter_contract(
    *,
    mapper_path: Path,
    partitions: int = 64,
) -> AdapterContract:
    """Measure the real ``adapters/jsonl_map.py`` module bytes for the
    partitioner/reducer/verifier digests — that module is where bucketing,
    slicing, and reduce/verify actually live (Day 4). ``mapper_path`` (the
    cooperative per-leaf inference executable, e.g. ``embed_mapper.py``) is
    a *separate* identity input folded into a leaf's ``globals_digest``
    (§18: "execution_spec + mapper + output_contract digests") — call
    ``mapper_source_digest(mapper_path)`` for that, not this function.
    """
    digest = _sha256_file(_adapter_module_path())
    return AdapterContract(
        adapter_id=CONTRACT_JSONL_MAP,
        partitioner_digest=digest,
        reducer_digest=digest,
        verifier_digest=digest,
        partitions=partitions,
    )


def mapper_source_digest(mapper_path: Path) -> Digest:
    """The cooperative mapper's own code identity — edits here must
    invalidate every leaf even though they never touch adapter bucketing."""
    return _sha256_file(mapper_path)
