"""Flight Recorder execution driver (Day 3) — plan → run → publish/restore.

Compatibility selector + previous-input re-resolution + semantic_work_key.
First opaque run never coalesces (CANDIDATE only). Second matching
qualification promotes CANDIDATE→VALIDATED. Third may restore. Trace-drift
supersedes. Hidden/new resource or network → force run / non-reusable.

Gate A freeze: arbitrary opaque exec stays ``SHADOW_UNQUALIFIED``; verified
reuse only under named contracts that pass Gate B.
"""

from __future__ import annotations

import hashlib
import os
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from cairn.db import flight as flight_db
from cairn.flight import identity as flight_identity
from cairn.flight.contracts import (
    CONTRACT_JSONL_MAP,
    CONTRACT_SHADOW,
    declared_coverage_for_contract,
)
from cairn.flight.restore import restore_output_atomic
from cairn.flight.types import (
    AccessMode,
    Authority,
    CoverageState,
    Digest,
    ExecutionSpec,
    ObservationSource,
    PlanAction,
    ResourceIdentity,
    ResourceKind,
    TraceContent,
)
from cairn.obs.events import emit_event
from cairn.storage.cas import (
    BlobIntegrityError,
    BlobUnavailable,
    ContentBlobRef,
    fetch_blob_verified,
    publish_blob,
)
from cairn.trace.scout import run_scout


@dataclass(frozen=True, slots=True)
class ExecPlan:
    action: PlanAction
    semantic_work_key: Digest | None
    compatibility_key: Digest
    generation: int | None = None
    claim: flight_db.GenerationClaim | None = None
    current: flight_db.CurrentDerivation | None = None
    candidate_observation_id: uuid.UUID | None = None
    reason: str = ""
    authorized_by: str | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    action: PlanAction
    child_exit_code: int
    coverage_state: CoverageState
    semantic_work_key: Digest | None
    observation_id: uuid.UUID | None
    derivation_id: uuid.UUID | None
    blob_digest: Digest | None
    authorized_by: str | None
    owner_id: str | None
    task_arn: str | None
    generation: int | None
    fence: int | None
    output_path: Path
    reasons: tuple[str, ...] = ()


def environment_digest(env_names: tuple[str, ...]) -> Digest:
    from cairn.fingerprint.canon import canonical_json

    return hashlib.sha256(
        canonical_json(
            {
                "label": "environment_names",
                "v": flight_identity.SCHEMA_VERSION,
                "body": list(env_names),
            }
        )
    ).hexdigest()


def resolve_workspace_inputs(
    resources: tuple[ResourceIdentity, ...],
    *,
    workspace: Path,
) -> tuple[ResourceIdentity, ...] | None:
    """Re-resolve prior INPUT resources against the live workspace.

    Returns None if any prior input fails to resolve identically (forces run).
    """
    resolved: list[ResourceIdentity] = []
    for resource in resources:
        if not resource.access_mode.is_input:
            continue
        if resource.kind is ResourceKind.FILE:
            path = workspace / resource.ref
            if resource.access_mode is AccessMode.NEGATIVE:
                if path.exists():
                    return None
                resolved.append(resource)
                continue
            if not path.is_file():
                return None
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != resource.version_digest:
                return None
            resolved.append(
                ResourceIdentity(
                    kind=resource.kind,
                    ref=resource.ref,
                    access_mode=resource.access_mode,
                    exists=True,
                    version_digest=digest,
                    resolver=resource.resolver,
                    source=ObservationSource.DECLARED,
                )
            )
        else:
            # Non-file inputs (env/s3/…) must still match the recorded digest;
            # live re-hash of opaque kinds is not invented here — equality of
            # the prior identity is required for the selector hit.
            resolved.append(resource)
    return tuple(sorted(resolved, key=lambda r: r.identity_tuple))


def declared_file_inputs(
    paths: Sequence[Path],
    *,
    workspace: Path,
) -> tuple[ResourceIdentity, ...]:
    rows: list[ResourceIdentity] = []
    root = workspace.resolve()
    for path in paths:
        p = path.resolve()
        rel = p.relative_to(root).as_posix()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append(
            ResourceIdentity(
                kind=ResourceKind.FILE,
                ref=rel,
                access_mode=AccessMode.READ,
                exists=True,
                version_digest=digest,
                resolver="workspace_file/v1",
                source=ObservationSource.DECLARED,
            )
        )
    return tuple(sorted(rows, key=lambda r: r.identity_tuple))


def compute_semantic_key(
    spec: ExecutionSpec,
    resolved_inputs: tuple[ResourceIdentity, ...],
) -> Digest:
    return flight_identity.semantic_work_key(
        spec_digest=flight_identity.spec_digest(spec),
        platform_digest=flight_identity.platform_digest(spec),
        environment_digest=environment_digest(spec.purity.env_names),
        resolved_inputs=resolved_inputs,
        adapter=spec.adapter,
        output_contract_digest=flight_identity.output_contract_digest(spec),
    )


def plan_execution(
    pool: ConnectionPool,
    *,
    spec: ExecutionSpec,
    workspace: Path,
    owner_id: str,
    host: str,
    region: str,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
    declared_inputs: tuple[ResourceIdentity, ...] | None = None,
    allow_coalesce: bool = False,
) -> ExecPlan:
    """§12 plan algorithm. Unknown opaque work never coalesces."""
    c_key = flight_identity.compatibility_key(spec)
    priors = flight_db.select_validated_observations(
        pool, namespace_id=spec.namespace_id, compatibility_key=c_key, limit=8
    )
    learning = flight_db.select_candidate_observations(
        pool, namespace_id=spec.namespace_id, compatibility_key=c_key, limit=8
    )

    resolved: tuple[ResourceIdentity, ...] | None = None
    if priors:
        for prior in priors:
            rows = flight_db.lookup_observation_resources(pool, trace_digest=prior.trace_digest)
            prior_inputs = tuple(
                ResourceIdentity(
                    kind=ResourceKind(kind),
                    ref=ref,
                    access_mode=AccessMode(mode),
                    exists=exists,
                    version_digest=digest,
                    resolver=resolver,
                    source=ObservationSource.DECLARED,
                )
                for kind, ref, mode, exists, digest, resolver in rows
                if AccessMode(mode).is_input
            )
            snap = resolve_workspace_inputs(prior_inputs, workspace=workspace)
            if snap is not None:
                resolved = snap
                break
        if resolved is None:
            return ExecPlan(
                action=PlanAction.REFUSE_REUSE,
                semantic_work_key=None,
                compatibility_key=c_key,
                reason="no prior validated input set re-resolves",
            )
    elif declared_inputs is not None and spec.adapter is not None:
        # Named adapter authority permits first-run coalescing, not future reuse.
        resolved = declared_inputs
        allow_coalesce = True
    elif learning:
        # Second opaque run: re-resolve the candidate's inputs for qualification.
        for cand in learning:
            rows = flight_db.lookup_observation_resources(pool, trace_digest=cand.trace_digest)
            cand_inputs = tuple(
                ResourceIdentity(
                    kind=ResourceKind(kind),
                    ref=ref,
                    access_mode=AccessMode(mode),
                    exists=exists,
                    version_digest=digest,
                    resolver=resolver,
                    source=ObservationSource.DECLARED,
                )
                for kind, ref, mode, exists, digest, resolver in rows
                if AccessMode(mode).is_input
            )
            snap = resolve_workspace_inputs(cand_inputs, workspace=workspace)
            if snap is not None:
                resolved = snap
                break
        if resolved is None:
            return ExecPlan(
                action=PlanAction.RUN_SHADOW_LEARN,
                semantic_work_key=None,
                compatibility_key=c_key,
                reason="candidate inputs no longer resolve; relearn",
            )
    else:
        # Unknown opaque work cannot coalesce/dispatch.
        return ExecPlan(
            action=PlanAction.RUN_SHADOW_LEARN,
            semantic_work_key=None,
            compatibility_key=c_key,
            reason="no validated prior; opaque first run never coalesces",
        )

    if spec.purity.contract_id == CONTRACT_SHADOW:
        return ExecPlan(
            action=PlanAction.RUN_SHADOW_LEARN,
            semantic_work_key=compute_semantic_key(spec, resolved) if resolved else None,
            compatibility_key=c_key,
            reason="shadow contract never authorizes reuse",
        )

    work = compute_semantic_key(spec, resolved)
    current_map = flight_db.current_derivations(
        pool, namespace_id=spec.namespace_id, keys=[work]
    )
    current = current_map.get(work)

    candidate = flight_db.find_candidate_observation(
        pool, namespace_id=spec.namespace_id, semantic_work_key=work
    )

    if current is None and candidate is not None:
        return ExecPlan(
            action=PlanAction.RUN_ISOLATED_QUALIFICATION,
            semantic_work_key=work,
            compatibility_key=c_key,
            candidate_observation_id=candidate[0],
            reason="candidate exists; second run must qualify before restore",
        )

    # Derivation published under CANDIDATE is not returned by current_derivations
    # (selector requires VALIDATED). Detect via generation head separately.
    if current is not None:
        return ExecPlan(
            action=PlanAction.RESTORE,
            semantic_work_key=work,
            compatibility_key=c_key,
            generation=current.generation,
            current=current,
            authorized_by="identity",
            reason="validated current derivation",
        )

    if not allow_coalesce and not priors:
        return ExecPlan(
            action=PlanAction.RUN_SHADOW_LEARN,
            semantic_work_key=work,
            compatibility_key=c_key,
            reason="first opaque run records candidate only",
        )

    claim = flight_db.open_generation(
        pool,
        namespace_id=spec.namespace_id,
        semantic_work_key=work,
        stage_label="exec",
        owner_id=owner_id,
        host=host,
        region=region,
        run_id=run_id,
        request_id=request_id,
    )
    if claim.role is flight_db.ClaimRole.RESTORE and claim.derivation_id is not None:
        # Re-check authority — SUCCEEDED alone is never enough.
        refreshed = flight_db.current_derivations(
            pool, namespace_id=spec.namespace_id, keys=[work]
        ).get(work)
        if refreshed is not None:
            return ExecPlan(
                action=PlanAction.RESTORE,
                semantic_work_key=work,
                compatibility_key=c_key,
                generation=claim.generation,
                claim=claim,
                current=refreshed,
                authorized_by="identity",
                reason="claim pointed at validated derivation",
            )
        # Candidate publication under SUCCEEDED — qualify, do not restore.
        return ExecPlan(
            action=PlanAction.RUN_ISOLATED_QUALIFICATION,
            semantic_work_key=work,
            compatibility_key=c_key,
            generation=claim.generation,
            claim=claim,
            candidate_observation_id=candidate[0] if candidate else None,
            reason="published derivation observation not yet VALIDATED",
        )
    if claim.role is flight_db.ClaimRole.SUBSCRIBER:
        return ExecPlan(
            action=PlanAction.SUBSCRIBE,
            semantic_work_key=work,
            compatibility_key=c_key,
            generation=claim.generation,
            claim=claim,
            reason="live owner holds generation",
        )
    if claim.took_over_from is not None:
        return ExecPlan(
            action=PlanAction.TAKE_OVER,
            semantic_work_key=work,
            compatibility_key=c_key,
            generation=claim.generation,
            claim=claim,
            reason="lease expired; fence transferred",
        )
    return ExecPlan(
        action=PlanAction.RUN_LOCAL,
        semantic_work_key=work,
        compatibility_key=c_key,
        generation=claim.generation,
        claim=claim,
        reason="acquired as owner",
    )


def _apply_declared_coverage(spec: ExecutionSpec, trace: TraceContent) -> TraceContent:
    state = declared_coverage_for_contract(spec.purity.contract_id, trace.coverage_state)
    if state is trace.coverage_state:
        return trace
    reasons = () if state.authorizes_reuse else trace.incomplete_reasons
    if state is CoverageState.COMPLETE_DECLARED:
        reasons = ()
    return TraceContent(
        coverage_state=state,
        resources=trace.resources,
        incomplete_reasons=reasons,
    )


def _detect_drift(
    predicted: tuple[ResourceIdentity, ...],
    actual: TraceContent,
) -> str | None:
    predicted_set = {r.identity_tuple + (r.version_digest, r.exists) for r in predicted}
    actual_inputs = {
        r.identity_tuple + (r.version_digest, r.exists)
        for r in actual.inputs
    }
    if actual.coverage_state is CoverageState.INCOMPLETE_NETWORK:
        return "network activity makes result non-reusable"
    # Hidden/new resource: actual has an input the prediction lacked.
    pred_refs = {r.identity_tuple for r in predicted}
    for resource in actual.inputs:
        if resource.identity_tuple not in pred_refs:
            return f"hidden/new resource {resource.ref!r} forces run"
    if predicted_set != actual_inputs:
        return "trace drift: resolved input set changed"
    return None


def restore_from_current(
    current: flight_db.CurrentDerivation,
    *,
    output_path: Path,
) -> Digest:
    """Fetch verified bytes and atomically restore. Raises on integrity/unavailable."""
    ref = ContentBlobRef(
        blob_digest=current.blob_digest,
        bucket=current.bucket,
        object_key=current.object_key,
        version_id=current.version_id,
        checksum_sha256=current.checksum_sha256,
        size_bytes=current.size_bytes,
    )
    data = fetch_blob_verified(ref)
    restore_output_atomic(output_path, data)
    return current.blob_digest


def execute(
    pool: ConnectionPool,
    *,
    spec: ExecutionSpec,
    argv: list[str],
    output_file: Path,
    workspace: Path | None = None,
    bucket: str | None = None,
    remote: str = "local",
    qualify: bool = False,
    declared_input_paths: tuple[Path, ...] = (),
    task_arn: str | None = None,
    owner_id: str | None = None,
) -> ExecResult:
    """Drive one ``cairn exec`` invocation end-to-end."""
    root = (workspace or Path.cwd()).resolve()
    host = socket.gethostname()
    region = (
        os.environ.get("CAIRN_WORKER_REGION") or os.environ.get("CAIRN_AWS_REGION") or "local"
    )
    owner = owner_id or f"local/{host}"
    run_id = uuid.uuid4()
    request_id = uuid.uuid4()
    out_path = (root / spec.output.path_rel).resolve()

    declared = (
        declared_file_inputs(declared_input_paths, workspace=root)
        if declared_input_paths
        else None
    )
    allow_coalesce = spec.adapter is not None and spec.purity.contract_id == CONTRACT_JSONL_MAP

    if qualify:
        return _run_qualify(
            pool,
            spec=spec,
            argv=argv,
            output_file=output_file,
            workspace=root,
            bucket=bucket,
            region=region,
            host=host,
            owner=owner,
            declared=declared,
            allow_coalesce=allow_coalesce,
            task_arn=task_arn,
        )

    plan = plan_execution(
        pool,
        spec=spec,
        workspace=root,
        owner_id=owner,
        host=host,
        region=region,
        run_id=run_id,
        request_id=request_id,
        declared_inputs=declared,
        allow_coalesce=allow_coalesce,
    )
    emit_event(
        "flight.plan",
        {
            "action": plan.action.value,
            "reason": plan.reason,
            "semantic_work_key": plan.semantic_work_key,
            "authorized_by": plan.authorized_by,
            "remote": remote,
        },
        run_id=str(run_id),
    )

    if plan.action is PlanAction.RESTORE and plan.current is not None:
        current = plan.current
        try:
            digest = restore_from_current(current, output_path=out_path)
        except BlobIntegrityError:
            flight_db.invalidate_generation(
                pool,
                namespace_id=spec.namespace_id,
                semantic_work_key=plan.semantic_work_key or "",
                reason="s3_blob_integrity_mismatch",
                mark_blob_invalid=current.blob_digest,
            )
            # Fall through to a fresh run after rollover.
            plan = ExecPlan(
                action=PlanAction.RUN_LOCAL,
                semantic_work_key=plan.semantic_work_key,
                compatibility_key=plan.compatibility_key,
                reason="S3 mismatch forced generation rollover",
            )
        except BlobUnavailable:
            plan = ExecPlan(
                action=PlanAction.RUN_LOCAL,
                semantic_work_key=plan.semantic_work_key,
                compatibility_key=plan.compatibility_key,
                reason="S3 transiently unavailable; run locally",
            )
        else:
            return ExecResult(
                action=PlanAction.RESTORE,
                child_exit_code=0,
                coverage_state=CoverageState(current.coverage_state or "COMPLETE_DECLARED"),
                semantic_work_key=plan.semantic_work_key,
                observation_id=current.observation_id,
                derivation_id=current.derivation_id,
                blob_digest=digest,
                authorized_by="identity",
                owner_id=current.owner_id,
                task_arn=current.task_arn,
                generation=current.generation,
                fence=None,
                output_path=out_path,
            )

    if plan.action is PlanAction.SUBSCRIBE and plan.claim is not None:
        # Day-3: durable interest is registered; wait via claim subscribe on
        # the generation claim key, then attempt restore if validated.
        from cairn.db import claims as claims_db

        flight_db.register_interest(
            pool,
            namespace_id=spec.namespace_id,
            semantic_work_key=plan.semantic_work_key or "",
            generation=plan.generation or 1,
            subscriber_id=request_id,
            request_id=request_id,
            run_id=run_id,
            joined_fence=plan.claim.fence or 1,
        )
        sub = claims_db.subscribe(pool, plan.claim.claim_key)
        if plan.semantic_work_key:
            subscribed = flight_db.current_derivations(
                pool, namespace_id=spec.namespace_id, keys=[plan.semantic_work_key]
            ).get(plan.semantic_work_key)
            if subscribed is not None and sub.terminal_state == "SUCCEEDED":
                digest = restore_from_current(subscribed, output_path=out_path)
                return ExecResult(
                    action=PlanAction.SUBSCRIBE,
                    child_exit_code=0,
                    coverage_state=CoverageState(
                        subscribed.coverage_state or "COMPLETE_DECLARED"
                    ),
                    semantic_work_key=plan.semantic_work_key,
                    observation_id=subscribed.observation_id,
                    derivation_id=subscribed.derivation_id,
                    blob_digest=digest,
                    authorized_by="identity",
                    owner_id=subscribed.owner_id,
                    task_arn=subscribed.task_arn,
                    generation=subscribed.generation,
                    fence=plan.claim.fence,
                    output_path=out_path,
                )

    if remote == "ecs" and spec.purity.contract_id != CONTRACT_JSONL_MAP:
        raise ValueError("ECS remote is only permitted for the bundled jsonl-map/v1 envelope")

    # RUN_* paths: scout/trace, then optionally publish.
    return _run_and_maybe_publish(
        pool,
        spec=spec,
        argv=argv,
        output_file=output_file,
        workspace=root,
        bucket=bucket,
        region=region,
        host=host,
        owner=owner,
        run_id=run_id,
        request_id=request_id,
        plan=plan,
        task_arn=task_arn,
        qualify_against=plan.candidate_observation_id,
    )


def _run_qualify(
    pool: ConnectionPool,
    *,
    spec: ExecutionSpec,
    argv: list[str],
    output_file: Path,
    workspace: Path,
    bucket: str | None,
    region: str,
    host: str,
    owner: str,
    declared: tuple[ResourceIdentity, ...] | None,
    allow_coalesce: bool,
    task_arn: str | None,
) -> ExecResult:
    """Two isolated runs inside one ``--qualify`` invocation; promote on match."""
    first = _run_and_maybe_publish(
        pool,
        spec=spec,
        argv=argv,
        output_file=output_file,
        workspace=workspace,
        bucket=bucket,
        region=region,
        host=host,
        owner=owner,
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        plan=ExecPlan(
            action=PlanAction.RUN_ISOLATED_QUALIFICATION,
            semantic_work_key=None,
            compatibility_key=flight_identity.compatibility_key(spec),
            reason="qualify run 1",
        ),
        task_arn=task_arn,
        force_learn=True,
        declared_inputs=declared,
        allow_coalesce=allow_coalesce,
    )
    if first.child_exit_code != 0:
        return first
    second = _run_and_maybe_publish(
        pool,
        spec=spec,
        argv=argv,
        output_file=output_file,
        workspace=workspace,
        bucket=bucket,
        region=region,
        host=host,
        owner=owner,
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        plan=ExecPlan(
            action=PlanAction.RUN_ISOLATED_QUALIFICATION,
            semantic_work_key=first.semantic_work_key,
            compatibility_key=flight_identity.compatibility_key(spec),
            candidate_observation_id=first.observation_id,
            reason="qualify run 2",
        ),
        task_arn=task_arn,
        force_learn=True,
        qualify_against=first.observation_id,
        expected_blob=first.blob_digest,
        declared_inputs=declared,
        allow_coalesce=allow_coalesce,
    )
    return second


def _run_and_maybe_publish(
    pool: ConnectionPool,
    *,
    spec: ExecutionSpec,
    argv: list[str],
    output_file: Path,
    workspace: Path,
    bucket: str | None,
    region: str,
    host: str,
    owner: str,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
    plan: ExecPlan,
    task_arn: str | None,
    force_learn: bool = False,
    qualify_against: uuid.UUID | None = None,
    expected_blob: Digest | None = None,
    declared_inputs: tuple[ResourceIdentity, ...] | None = None,
    allow_coalesce: bool = False,
) -> ExecResult:
    scout = run_scout(
        argv,
        output_file=output_file,
        workspace=workspace,
        namespace_id=spec.namespace_id,
        image_digest=spec.platform.image_digest,
        task_arn=task_arn,
    )
    # Rebuild spec from scout platform bits but keep caller contract/argv/adapter.
    traced_spec = ExecutionSpec(
        namespace_id=spec.namespace_id,
        argv=spec.argv,
        cwd_rel=spec.cwd_rel,
        output=spec.output,
        platform=spec.platform,
        purity=spec.purity,
        adapter=spec.adapter,
        tracer_version=spec.tracer_version,
        companion_version=spec.companion_version,
        adapter_registry_version=spec.adapter_registry_version,
    )
    trace = _apply_declared_coverage(traced_spec, scout.trace)
    out_path = (workspace / traced_spec.output.path_rel).resolve()

    # Network / incomplete → non-reusable learning only.
    reusable = (
        traced_spec.purity.contract_id != CONTRACT_SHADOW
        and trace.coverage_state.authorizes_reuse
    )

    predicted = declared_inputs
    if predicted and reusable:
        drift = _detect_drift(predicted, trace)
        if drift:
            if plan.semantic_work_key:
                flight_db.invalidate_generation(
                    pool,
                    namespace_id=spec.namespace_id,
                    semantic_work_key=plan.semantic_work_key,
                    reason=drift,
                )
            reusable = False
            new_state = (
                CoverageState.INCOMPLETE_NETWORK
                if "network" in drift
                else CoverageState.NONDETERMINISTIC
            )
            trace = TraceContent(
                coverage_state=new_state,
                resources=trace.resources,
                incomplete_reasons=tuple(
                    dict.fromkeys((*trace.incomplete_reasons, drift))
                ),
            )

    persisted = flight_db.persist_candidate_observation(
        pool,
        namespace_id=spec.namespace_id,
        spec=traced_spec,
        trace=trace,
        run_id=run_id,
        region=region,
        task_arn=task_arn,
        work_key=plan.semantic_work_key or f"exec:{flight_identity.spec_digest(traced_spec)[:16]}",
    )
    sem_key = plan.semantic_work_key or compute_semantic_key(traced_spec, trace.inputs)

    blob_digest: Digest | None = None
    derivation_id: uuid.UUID | None = None
    generation = plan.generation
    fence = plan.claim.fence if plan.claim else None
    authorized_by: str | None = None
    action = plan.action

    if (
        qualify_against is not None
        and scout.child_exit_code == 0
        and out_path.is_file()
        and reusable
    ):
        output_bytes = out_path.read_bytes()
        out_digest = hashlib.sha256(output_bytes).hexdigest()
        if expected_blob is not None and out_digest != expected_blob:
            flight_db.invalidate_generation(
                pool,
                namespace_id=spec.namespace_id,
                semantic_work_key=sem_key,
                reason="qualification_output_mismatch",
            )
            return ExecResult(
                action=PlanAction.REFUSE_REUSE,
                child_exit_code=3,
                coverage_state=CoverageState.NONDETERMINISTIC,
                semantic_work_key=sem_key,
                observation_id=persisted.observation_id,
                derivation_id=None,
                blob_digest=out_digest,
                authorized_by=None,
                owner_id=owner,
                task_arn=task_arn,
                generation=generation,
                fence=fence,
                output_path=out_path,
                reasons=("qualification digests do not match",),
            )
        # Promote the *prior* candidate; this run is the validator.
        promoted = flight_db.promote_observation(
            pool,
            observation_id=qualify_against,
            validator_run_id=run_id,
            namespace_id=spec.namespace_id,
        )
        if promoted:
            authorized_by = None  # promotion is not yet a restore
            action = PlanAction.RUN_ISOLATED_QUALIFICATION

    # Publish whole-result when we own a generation and coverage authorizes learning publish.
    should_publish = (
        scout.child_exit_code == 0
        and out_path.is_file()
        and bucket
        and reusable
        and (force_learn or plan.claim is not None or plan.action in {
            PlanAction.RUN_LOCAL,
            PlanAction.TAKE_OVER,
            PlanAction.RUN_ISOLATED_QUALIFICATION,
            PlanAction.RUN_SHADOW_LEARN,
        })
        and traced_spec.purity.contract_id != CONTRACT_SHADOW
    )
    # Opaque first learn publishes candidate reachability only when we acquired
    # or when qualify/force_learn is recording the first of two runs.
    if should_publish and plan.claim is None and (force_learn or allow_coalesce):
        claim = flight_db.open_generation(
            pool,
            namespace_id=spec.namespace_id,
            semantic_work_key=sem_key,
            stage_label="exec",
            owner_id=owner,
            host=host,
            region=region,
            run_id=run_id,
            request_id=request_id,
        )
        if claim.role is flight_db.ClaimRole.OWNER:
            generation = claim.generation
            fence = claim.fence
            plan_claim = claim
        else:
            plan_claim = None
            should_publish = False
    else:
        plan_claim = plan.claim

    if (
        should_publish
        and plan_claim is not None
        and fence is not None
        and generation is not None
        and bucket is not None
    ):
        output_bytes = out_path.read_bytes()
        publication = publish_blob(bucket, output_bytes)
        blob_digest = publication.blob_digest
        derivation_id = uuid.uuid4()
        pub_op = uuid.uuid4()
        outcome = flight_db.publish_derivation(
            pool,
            namespace_id=spec.namespace_id,
            semantic_work_key=sem_key,
            generation=generation,
            derivation_id=derivation_id,
            publication_operation_id=pub_op,
            blob=publication,
            observation_id=qualify_against or persisted.observation_id,
            authority=Authority.IDENTITY,
            run_id=run_id,
            owner_id=owner,
            fence=fence,
            decision_explanation="whole-result publication under named contract",
        )
        if outcome is flight_db.PublishOutcome.REJECTED_FENCE:
            derivation_id = None
            blob_digest = publication.blob_digest

    return ExecResult(
        action=action if action is not PlanAction.REFUSE_REUSE else PlanAction.RUN_SHADOW_LEARN,
        child_exit_code=scout.child_exit_code,
        coverage_state=trace.coverage_state,
        semantic_work_key=sem_key,
        observation_id=persisted.observation_id,
        derivation_id=derivation_id,
        blob_digest=blob_digest,
        authorized_by=authorized_by,
        owner_id=owner,
        task_arn=task_arn,
        generation=generation,
        fence=fence,
        output_path=out_path,
        reasons=trace.incomplete_reasons,
    )
