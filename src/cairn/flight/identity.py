"""Flight Recorder identity digests (Appendix B.2).

Every digest is ``sha256(canonical_json({label, v, body}))`` so Flight
Recorder shares ``fingerprint.canon.canonical_json`` with the five-stage
path. A second JSON canonicalizer would be a second place for encoding to
drift — and every published result depends on these digests staying stable.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256

from cairn.fingerprint.canon import canonical_json
from cairn.flight.types import (
    AdapterContract,
    Digest,
    ExecutionSpec,
    ResourceIdentity,
    TraceContent,
)

SCHEMA_VERSION = "cairn/flight/v1"


def _digest(label: str, payload: object) -> Digest:
    return sha256(
        canonical_json({"label": label, "v": SCHEMA_VERSION, "body": payload})
    ).hexdigest()


def _resource_payloads(resources: Sequence[ResourceIdentity]) -> list[dict[str, object]]:
    return [r.identity_payload() for r in sorted(resources, key=lambda x: x.identity_tuple)]


def _adapter_payload(adapter: AdapterContract | None) -> dict[str, object] | None:
    if adapter is None:
        return None
    return {
        "adapter_id": adapter.adapter_id,
        "partitioner_digest": adapter.partitioner_digest,
        "reducer_digest": adapter.reducer_digest,
        "verifier_digest": adapter.verifier_digest,
        "partitions": adapter.partitions,
    }


def _spec_body(spec: ExecutionSpec) -> dict[str, object]:
    return {
        "namespace_id": spec.namespace_id,
        "argv": list(spec.argv),
        "cwd_rel": spec.cwd_rel,
        "output": {"path_rel": spec.output.path_rel, "kind": spec.output.kind},
        "platform": {
            "os": spec.platform.os,
            "arch": spec.platform.arch,
            "image_digest": spec.platform.image_digest,
        },
        "purity": {
            "contract_id": spec.purity.contract_id,
            "network": spec.purity.network,
            "clock": spec.purity.clock,
            "random": spec.purity.random,
            "env_names": list(spec.purity.env_names),
            "qualification_runs": spec.purity.qualification_runs,
        },
        "adapter": _adapter_payload(spec.adapter),
        "tracer_version": spec.tracer_version,
        "companion_version": spec.companion_version,
        "adapter_registry_version": spec.adapter_registry_version,
    }


def spec_digest(spec: ExecutionSpec) -> Digest:
    return _digest("spec", _spec_body(spec))


def coverage_profile_digest(spec: ExecutionSpec) -> Digest:
    return _digest(
        "coverage_profile",
        {
            "tracer_version": spec.tracer_version,
            "companion_version": spec.companion_version,
            "adapter_registry_version": spec.adapter_registry_version,
            "purity": {
                "contract_id": spec.purity.contract_id,
                "network": spec.purity.network,
                "clock": spec.purity.clock,
                "random": spec.purity.random,
                "env_names": list(spec.purity.env_names),
            },
            "adapter": _adapter_payload(spec.adapter),
            "platform_policy": {
                "os": spec.platform.os,
                "arch": spec.platform.arch,
                "image_pinned": spec.platform.image_digest is not None,
            },
        },
    )


def compatibility_key(spec: ExecutionSpec) -> Digest:
    """Shape-compatible executions share this key; exact reuse needs more."""
    return _digest(
        "compatibility_key",
        {
            "argv": list(spec.argv),
            "cwd_rel": spec.cwd_rel,
            "output": {"path_rel": spec.output.path_rel, "kind": spec.output.kind},
            "purity": {
                "contract_id": spec.purity.contract_id,
                "network": spec.purity.network,
                "clock": spec.purity.clock,
                "random": spec.purity.random,
                "env_names": list(spec.purity.env_names),
            },
            "adapter": _adapter_payload(spec.adapter),
            "tracer_version": spec.tracer_version,
            "companion_version": spec.companion_version,
            "adapter_registry_version": spec.adapter_registry_version,
            "platform": {
                "os": spec.platform.os,
                "arch": spec.platform.arch,
                "image_digest": spec.platform.image_digest,
            },
        },
    )


def input_resource_set_digest(trace: TraceContent) -> Digest:
    return _digest("input_resource_set", _resource_payloads(trace.inputs))


def output_evidence_digest(trace: TraceContent) -> Digest:
    return _digest("output_evidence", _resource_payloads(trace.outputs))


def trace_digest(trace: TraceContent) -> Digest:
    return _digest(
        "trace",
        {
            "coverage_state": trace.coverage_state.value,
            "incomplete_reasons": list(trace.incomplete_reasons),
            "resources": _resource_payloads(trace.resources),
        },
    )


def semantic_work_key(
    *,
    spec_digest: Digest,
    platform_digest: Digest,
    environment_digest: Digest,
    resolved_inputs: Sequence[ResourceIdentity],
    adapter: AdapterContract | None,
    output_contract_digest: Digest,
) -> Digest:
    """Work identity from observed inputs + contracts — never Git SHA / path / host / clock."""
    return _digest(
        "semantic_work_key",
        {
            "spec_digest": spec_digest,
            "platform_digest": platform_digest,
            "environment_digest": environment_digest,
            "resolved_inputs": _resource_payloads(resolved_inputs),
            "adapter": _adapter_payload(adapter),
            "output_contract_digest": output_contract_digest,
        },
    )


def claim_key(namespace_id: str, semantic_work_key: Digest, generation: int) -> Digest:
    if generation < 1:
        raise ValueError("generation must be >= 1")
    return _digest(
        "claim_key",
        {
            "namespace_id": namespace_id,
            "semantic_work_key": semantic_work_key,
            "generation": generation,
        },
    )


def platform_digest(spec: ExecutionSpec) -> Digest:
    return _digest(
        "platform",
        {
            "os": spec.platform.os,
            "arch": spec.platform.arch,
            "image_digest": spec.platform.image_digest,
        },
    )


def output_contract_digest(spec: ExecutionSpec) -> Digest:
    return _digest(
        "output_contract",
        {"path_rel": spec.output.path_rel, "kind": spec.output.kind},
    )
