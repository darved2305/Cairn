"""Frozen golden vectors for Flight Recorder identity digests.

Changing any of these digests invalidates every previously published result —
the test exists to make that change loud. Two product properties are asserted
directly: Git SHA / absolute path / hostname / clock do NOT enter
semantic_work_key; one observed input change DOES.
"""

from __future__ import annotations

import inspect

from cairn.flight import identity
from cairn.flight.types import (
    AccessMode,
    CoverageState,
    ExecutionSpec,
    ObservationSource,
    OutputContract,
    PlatformContract,
    PurityPolicy,
    ResourceIdentity,
    ResourceKind,
    TraceContent,
)

_SPEC = ExecutionSpec(
    namespace_id="cairn-demo",
    argv=("python", "build_features.py"),
    cwd_rel=".",
    output=OutputContract(path_rel="artifacts/features.bin"),
    platform=PlatformContract(
        os="linux",
        arch="amd64",
        image_digest="sha256:" + ("ab" * 32),
    ),
    purity=PurityPolicy(
        contract_id="deterministic-file/v1",
        network="deny",
        clock="deny",
        random="deny",
        env_names=("LANG", "LC_ALL"),
        qualification_runs=2,
    ),
    adapter=None,
    tracer_version="strace/v1",
    companion_version="python-audit/v1",
    adapter_registry_version="adapters/v1",
)

_INPUT_A = ResourceIdentity(
    kind=ResourceKind.FILE,
    ref="data/input.jsonl",
    access_mode=AccessMode.READ,
    exists=True,
    version_digest="aa" * 32,
    resolver="workspace_file/v1",
    source=ObservationSource.SYSCALL,
    metadata=(("size", "12"),),
)

_INPUT_B = ResourceIdentity(
    kind=ResourceKind.FILE,
    ref="data/input.jsonl",
    access_mode=AccessMode.READ,
    exists=True,
    version_digest="bb" * 32,
    resolver="workspace_file/v1",
    source=ObservationSource.SYSCALL,
)

# Frozen 2026-08-09 against SCHEMA_VERSION = cairn/flight/v1.
GOLDEN_SPEC_DIGEST = "bb25fc8b0513a5dfbea5678a063d8c07814839f0979b9845886728e3638fbfc4"
GOLDEN_COMPATIBILITY_KEY = "80f7e0157da8b9f6621d16eb8b41c58373b56d57471d3f2a32b779870de5af68"
GOLDEN_SEMANTIC_WORK_KEY = "8ebfb9da1fd87d24d8b18fb8d43978b2acbd25bf7d56310947ae4151381b1728"
GOLDEN_CLAIM_KEY = "c5f58e3b5a7b28f145595743044c386425ea5a0918942896ee0884e57ae663e8"


def _semantic(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "spec_digest": identity.spec_digest(_SPEC),
        "platform_digest": identity.platform_digest(_SPEC),
        "environment_digest": "ee" * 32,
        "resolved_inputs": (_INPUT_A,),
        "adapter": None,
        "output_contract_digest": identity.output_contract_digest(_SPEC),
    }
    kwargs.update(overrides)
    return identity.semantic_work_key(**kwargs)  # type: ignore[arg-type]


def test_frozen_golden_vectors() -> None:
    assert identity.spec_digest(_SPEC) == GOLDEN_SPEC_DIGEST
    assert identity.compatibility_key(_SPEC) == GOLDEN_COMPATIBILITY_KEY
    assert _semantic() == GOLDEN_SEMANTIC_WORK_KEY
    assert identity.claim_key("cairn-demo", GOLDEN_SEMANTIC_WORK_KEY, 1) == GOLDEN_CLAIM_KEY


def test_semantic_work_key_ignores_git_path_host_clock() -> None:
    params = set(inspect.signature(identity.semantic_work_key).parameters)
    assert params.isdisjoint({"git_sha", "hostname", "clock", "cwd_abs", "workspace", "path"})
    assert not any("git" in p or "host" in p or "clock" in p or "path" in p for p in params)
    # Reconstructing with identical hashed inputs is stable regardless of
    # ambient Git SHA / absolute cwd / hostname / wall clock in the process.
    assert _semantic() == GOLDEN_SEMANTIC_WORK_KEY
    assert _semantic() == _semantic()


def test_semantic_work_key_changes_when_one_input_changes() -> None:
    assert _semantic(resolved_inputs=(_INPUT_B,)) != GOLDEN_SEMANTIC_WORK_KEY
    negative = ResourceIdentity(
        kind=ResourceKind.FILE,
        ref="data/input.jsonl",
        access_mode=AccessMode.NEGATIVE,
        exists=False,
        version_digest="absent",
        resolver="workspace_file/v1",
        source=ObservationSource.SYSCALL,
    )
    assert _semantic(resolved_inputs=(negative,)) != GOLDEN_SEMANTIC_WORK_KEY
    with_extra = _semantic(
        resolved_inputs=(
            _INPUT_A,
            ResourceIdentity(
                kind=ResourceKind.FILE,
                ref="data/other.jsonl",
                access_mode=AccessMode.READ,
                exists=True,
                version_digest="cc" * 32,
                resolver="workspace_file/v1",
                source=ObservationSource.SYSCALL,
            ),
        )
    )
    assert with_extra != GOLDEN_SEMANTIC_WORK_KEY


def test_trace_digests_stable() -> None:
    trace = TraceContent(
        coverage_state=CoverageState.COMPLETE_DECLARED,
        resources=(_INPUT_A,),
        incomplete_reasons=(),
    )
    assert identity.trace_digest(trace) == identity.trace_digest(trace)
    assert len(identity.input_resource_set_digest(trace)) == 64
