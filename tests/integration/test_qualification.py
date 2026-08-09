"""Day 3 Gate B qualification protocol (Appendix E).

Run 1 records a candidate; run 2 promotes it; run 3 may restore.
An unrelated Git SHA does not enter identity; a hidden new resource forces
a run; network is non-reusable; S3 mismatch forces generation rollover.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

from cairn.db import flight as flight_db
from cairn.flight.contracts import (
    CONTRACT_DETERMINISTIC_FILE,
    CONTRACT_JSONL_MAP,
    CONTRACT_SHADOW,
    build_execution_spec,
    jsonl_map_adapter_contract,
)
from cairn.flight.executor import (
    compute_semantic_key,
    plan_execution,
    resolve_workspace_inputs,
)
from cairn.flight.types import (
    AccessMode,
    Authority,
    CoverageState,
    ObservationSource,
    OutputContract,
    PlanAction,
    PlatformContract,
    PurityPolicy,
    ResourceIdentity,
    ResourceKind,
    TraceContent,
)
from cairn.storage import cas
from cairn.trace.collector import TRACER_VERSION
from cairn.trace.companion import COMPANION_VERSION
from cairn.trace.scout import ADAPTER_REGISTRY_VERSION

pytestmark = pytest.mark.integration


def _platform() -> PlatformContract:
    return PlatformContract(os="linux", arch="amd64", image_digest="sha256:" + "ab" * 32)


def _file_resource(ref: str, data: bytes) -> ResourceIdentity:
    return ResourceIdentity(
        kind=ResourceKind.FILE,
        ref=ref,
        access_mode=AccessMode.READ,
        exists=True,
        version_digest=hashlib.sha256(data).hexdigest(),
        resolver="workspace_file/v1",
        source=ObservationSource.DECLARED,
    )


def _write_resource(workspace: Path, ref: str, data: bytes) -> ResourceIdentity:
    path = workspace / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _file_resource(ref, data)


def _spec(
    *,
    namespace_id: str,
    contract_id: str = CONTRACT_DETERMINISTIC_FILE,
    argv: tuple[str, ...] = ("python", "tools/pure_write.py"),
    adapter=None,
):
    return build_execution_spec(
        contract_id=contract_id,
        argv=argv,
        cwd_rel=".",
        output_rel=".cairn/out/qual.bin",
        namespace_id=namespace_id,
        platform=_platform(),
        adapter=adapter,
    )


def test_opaque_first_run_never_coalesces(pool, tmp_path: Path) -> None:
    ns = f"qual-opaque-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(pool, ns, ns)
    spec = _spec(namespace_id=ns, contract_id=CONTRACT_SHADOW)
    plan = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="tester",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    assert plan.action is PlanAction.RUN_SHADOW_LEARN
    assert "never coalesces" in plan.reason or "opaque" in plan.reason


def test_candidate_second_qualify_then_third_restores(pool, tmp_path: Path) -> None:
    ns = f"qual-promo-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(pool, ns, ns)

    input_bytes = b"qualify-input-v1\n"
    resource = _write_resource(tmp_path, "data/in.txt", input_bytes)
    output_bytes = b"qualify-output-v1\n"
    out_path = tmp_path / ".cairn" / "out" / "qual.bin"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(output_bytes)

    spec = _spec(namespace_id=ns)
    trace = TraceContent(
        coverage_state=CoverageState.COMPLETE_DECLARED,
        resources=(
            resource,
            ResourceIdentity(
                kind=ResourceKind.FILE,
                ref=".cairn/out/qual.bin",
                access_mode=AccessMode.WRITE,
                exists=True,
                version_digest=hashlib.sha256(output_bytes).hexdigest(),
                resolver="workspace_file/v1",
                source=ObservationSource.DECLARED,
            ),
        ),
        incomplete_reasons=(),
    )
    run1 = uuid.uuid4()
    cand = flight_db.persist_candidate_observation(
        pool,
        namespace_id=ns,
        spec=spec,
        trace=trace,
        run_id=run1,
        region="local",
    )
    assert cand.lifecycle_state == "CANDIDATE"
    sem = compute_semantic_key(spec, (resource,))

    # First opaque/deterministic without prior VALIDATED → learn, no coalesce.
    plan1 = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="tester",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    assert plan1.action in {
        PlanAction.RUN_SHADOW_LEARN,
        PlanAction.RUN_ISOLATED_QUALIFICATION,
        PlanAction.REFUSE_REUSE,
    }

    # Metadata-only blob publication (S3 bytes are a separate gate).
    digest = hashlib.sha256(output_bytes).hexdigest()
    import base64

    publication = cas.BlobPublication(
        blob_digest=digest,
        bucket="cairn-test-meta",
        object_key=f"cas/sha256/{digest}",
        version_id=f"v-{uuid.uuid4().hex}",
        checksum_sha256=base64.b64encode(hashlib.sha256(output_bytes).digest()).decode(),
        size_bytes=len(output_bytes),
        already_existed=False,
    )
    owner = "tester-owner"
    run_pub = uuid.uuid4()
    req = uuid.uuid4()
    claim = flight_db.open_generation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem,
        stage_label="exec",
        owner_id=owner,
        host="host",
        region="local",
        run_id=run_pub,
        request_id=req,
    )
    assert claim.role is flight_db.ClaimRole.OWNER
    der_id = uuid.uuid4()
    outcome = flight_db.publish_derivation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem,
        generation=claim.generation,
        derivation_id=der_id,
        publication_operation_id=uuid.uuid4(),
        blob=publication,
        observation_id=cand.observation_id,
        authority=Authority.IDENTITY,
        run_id=run_pub,
        owner_id=owner,
        fence=claim.fence or 1,
    )
    assert outcome is flight_db.PublishOutcome.PUBLISHED

    # Selector must NOT restore while still CANDIDATE.
    assert flight_db.current_derivations(pool, namespace_id=ns, keys=[sem]) == {}

    plan_q = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="tester-2",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    assert plan_q.action is PlanAction.RUN_ISOLATED_QUALIFICATION

    # Second run promotes.
    run2 = uuid.uuid4()
    assert flight_db.promote_observation(
        pool,
        observation_id=cand.observation_id,
        validator_run_id=run2,
        namespace_id=ns,
    )
    current = flight_db.current_derivations(pool, namespace_id=ns, keys=[sem])
    assert sem in current
    assert current[sem].blob_digest == publication.blob_digest

    plan3 = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="tester-3",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    assert plan3.action is PlanAction.RESTORE
    assert plan3.authorized_by == "identity"


def test_declared_adapter_may_coalesce_but_not_reuse_candidate(pool, tmp_path: Path) -> None:
    ns = f"qual-adapt-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(pool, ns, ns)
    mapper = Path("examples/embed_mapper.py")
    if not mapper.is_file():
        pytest.skip("bundled mapper missing")
    adapter = jsonl_map_adapter_contract(mapper_path=mapper, partitions=64)
    data = b'{"id":"1"}\n'
    resource = _write_resource(tmp_path, "data/rows.jsonl", data)
    spec = build_execution_spec(
        contract_id=CONTRACT_JSONL_MAP,
        argv=("python", "/workspace/examples/embed_mapper.py"),
        cwd_rel=".",
        output_rel=".cairn/out/features.jsonl",
        namespace_id=ns,
        platform=_platform(),
        adapter=adapter,
    )
    plan = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="adapter-owner",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        declared_inputs=(resource,),
        allow_coalesce=True,
    )
    # First live work may acquire (coalesce), never RESTORE from nothing.
    assert plan.action in {
        PlanAction.RUN_LOCAL,
        PlanAction.TAKE_OVER,
        PlanAction.SUBSCRIBE,
    }
    assert plan.action is not PlanAction.RESTORE

    # Seed a CANDIDATE observation for the semantic key — still no restore.
    sem = compute_semantic_key(spec, (resource,))
    trace = TraceContent(
        coverage_state=CoverageState.COMPLETE_DECLARED,
        resources=(resource,),
        incomplete_reasons=(),
    )
    flight_db.persist_candidate_observation(
        pool,
        namespace_id=ns,
        spec=spec,
        trace=trace,
        run_id=uuid.uuid4(),
        region="local",
    )
    plan2 = plan_execution(
        pool,
        spec=spec,
        workspace=tmp_path,
        owner_id="adapter-owner-2",
        host="host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        declared_inputs=(resource,),
        allow_coalesce=True,
    )
    assert plan2.action is not PlanAction.RESTORE
    assert plan2.action in {
        PlanAction.RUN_ISOLATED_QUALIFICATION,
        PlanAction.RUN_LOCAL,
        PlanAction.SUBSCRIBE,
        PlanAction.TAKE_OVER,
        PlanAction.RUN_SHADOW_LEARN,
    }
    _ = sem


def test_git_sha_does_not_enter_identity(tmp_path: Path) -> None:
    resource = _write_resource(tmp_path, "data/x.txt", b"x")
    spec = _spec(namespace_id="git-inv")
    key_a = compute_semantic_key(spec, (resource,))
    # Simulate unrelated Git metadata sitting beside the workspace — it must
    # not be hashed into semantic identity (identity helpers never take it).
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    key_b = compute_semantic_key(spec, (resource,))
    assert key_a == key_b
    # Changing an observed input must change the key.
    resource2 = _write_resource(tmp_path, "data/x.txt", b"y")
    key_c = compute_semantic_key(spec, (resource2,))
    assert key_c != key_a


def test_hidden_resource_forces_run(tmp_path: Path) -> None:
    predicted = (_write_resource(tmp_path, "data/a.txt", b"a"),)
    # Workspace gains a new file the prediction did not include — resolver
    # still matches predicted paths, but executor drift detection catches
    # extras when actual trace includes them.
    from cairn.flight.executor import _detect_drift

    actual = TraceContent(
        coverage_state=CoverageState.COMPLETE_DECLARED,
        resources=(
            predicted[0],
            _write_resource(tmp_path, "data/hidden.txt", b"secret"),
        ),
        incomplete_reasons=(),
    )
    reason = _detect_drift(predicted, actual)
    assert reason is not None
    assert "hidden" in reason or "new resource" in reason


def test_network_forces_non_reusable() -> None:
    from cairn.flight.executor import _detect_drift

    predicted = (
        ResourceIdentity(
            kind=ResourceKind.FILE,
            ref="data/a.txt",
            access_mode=AccessMode.READ,
            exists=True,
            version_digest="ab" * 32,
            resolver="workspace_file/v1",
            source=ObservationSource.DECLARED,
        ),
    )
    actual = TraceContent(
        coverage_state=CoverageState.INCOMPLETE_NETWORK,
        resources=predicted,
        incomplete_reasons=("socket connect observed",),
    )
    reason = _detect_drift(predicted, actual)
    assert reason is not None
    assert "network" in reason


def test_s3_mismatch_forces_generation_rollover(pool) -> None:
    ns = f"qual-s3-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(pool, ns, ns)

    data = b"s3-mismatch-proof\n"
    digest = hashlib.sha256(data).hexdigest()
    import base64

    publication = cas.BlobPublication(
        blob_digest=digest,
        bucket="cairn-test-meta",
        object_key=f"cas/sha256/{digest}",
        version_id=f"v-{uuid.uuid4().hex}",
        checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode(),
        size_bytes=len(data),
        already_existed=False,
    )
    platform = _platform()
    spec = ExecutionSpec_for_test(ns, platform)
    resource = ResourceIdentity(
        kind=ResourceKind.FILE,
        ref="data/z.txt",
        access_mode=AccessMode.READ,
        exists=True,
        version_digest="cd" * 32,
        resolver="workspace_file/v1",
        source=ObservationSource.DECLARED,
    )
    sem = compute_semantic_key(spec, (resource,))
    owner = "s3-owner"
    run_id = uuid.uuid4()
    claim = flight_db.open_generation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem,
        stage_label="exec",
        owner_id=owner,
        host="host",
        region="local",
        run_id=run_id,
        request_id=uuid.uuid4(),
    )
    trace = TraceContent(
        coverage_state=CoverageState.COMPLETE_DECLARED,
        resources=(resource,),
        incomplete_reasons=(),
    )
    cand = flight_db.persist_candidate_observation(
        pool, namespace_id=ns, spec=spec, trace=trace, run_id=run_id, region="local"
    )
    flight_db.promote_observation(
        pool,
        observation_id=cand.observation_id,
        validator_run_id=uuid.uuid4(),
        namespace_id=ns,
    )
    outcome = flight_db.publish_derivation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem,
        generation=claim.generation,
        derivation_id=uuid.uuid4(),
        publication_operation_id=uuid.uuid4(),
        blob=publication,
        observation_id=cand.observation_id,
        authority=Authority.IDENTITY,
        run_id=run_id,
        owner_id=owner,
        fence=claim.fence or 1,
    )
    assert outcome is flight_db.PublishOutcome.PUBLISHED
    new_gen = flight_db.invalidate_generation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem,
        reason="s3_blob_integrity_mismatch",
        mark_blob_invalid=publication.blob_digest,
    )
    assert new_gen == claim.generation + 1
    assert flight_db.current_derivations(pool, namespace_id=ns, keys=[sem]) == {}


def ExecutionSpec_for_test(namespace_id: str, platform: PlatformContract):
    from cairn.flight.types import ExecutionSpec

    return ExecutionSpec(
        namespace_id=namespace_id,
        argv=("python", "x.py"),
        cwd_rel=".",
        output=OutputContract(path_rel=".cairn/out/qual.bin"),
        platform=platform,
        purity=PurityPolicy(
            contract_id=CONTRACT_DETERMINISTIC_FILE,
            network="deny",
            clock="deny",
            random="deny",
            env_names=(),
            qualification_runs=2,
        ),
        adapter=None,
        tracer_version=TRACER_VERSION,
        companion_version=COMPANION_VERSION,
        adapter_registry_version=ADAPTER_REGISTRY_VERSION,
    )


def test_resolve_workspace_inputs_detects_mutation(tmp_path: Path) -> None:
    resource = _write_resource(tmp_path, "data/m.txt", b"one")
    assert resolve_workspace_inputs((resource,), workspace=tmp_path) is not None
    (tmp_path / "data" / "m.txt").write_bytes(b"two")
    assert resolve_workspace_inputs((resource,), workspace=tmp_path) is None
