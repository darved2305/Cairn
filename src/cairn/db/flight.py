"""Flight Recorder database primitives (Appendix B.4).

Every function is an ``in_txn`` closure: pure in its arguments, no S3 calls,
no event emission. Events fire after the call returns. ``publish_derivation``
is the sole writer of reachability — nothing else may point a generation at
a derivation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cairn.db.claims import LEASE_SECONDS
from cairn.db.txn import in_txn
from cairn.flight import identity as flight_identity
from cairn.flight.types import Authority, Digest, ExecutionSpec, TraceContent
from cairn.obs.events import emit_event
from cairn.storage.cas import BlobPublication


class MicrochunkOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    REJECTED_FENCE = "REJECTED_FENCE"
    NONDETERMINISTIC = "NONDETERMINISTIC"


class ClaimRole(StrEnum):
    OWNER = "owner"
    SUBSCRIBER = "subscriber"
    RESTORE = "restore"


class InterestOutcome(StrEnum):
    REGISTERED = "REGISTERED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    DETACHED = "DETACHED"
    LAST_INTEREST = "LAST_INTEREST"
    ALREADY_DETACHED = "ALREADY_DETACHED"
    NOT_FOUND = "NOT_FOUND"


class PublishOutcome(StrEnum):
    PUBLISHED = "PUBLISHED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    REJECTED_FENCE = "REJECTED_FENCE"
    CONFLICT = "CONFLICT"
    STALE_AUTHORITY = "STALE_AUTHORITY"


@dataclass(frozen=True, slots=True)
class GenerationClaim:
    role: ClaimRole
    generation: int
    claim_key: Digest
    fence: int | None = None
    took_over_from: str | None = None
    owner_id: str | None = None
    owner_host: str | None = None
    owner_region: str | None = None
    owner_run_id: uuid.UUID | None = None
    derivation_id: uuid.UUID | None = None
    blob_digest: Digest | None = None


@dataclass(frozen=True, slots=True)
class LeafEdge:
    partition_key: str
    ordinal: int
    child_derivation_id: uuid.UUID
    input_slice_digest: Digest


@dataclass(frozen=True, slots=True)
class RuleRevisionRef:
    rule_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class CurrentDerivation:
    semantic_work_key: Digest
    generation: int
    derivation_id: uuid.UUID
    blob_digest: Digest
    bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int
    observation_id: uuid.UUID | None
    observation_lifecycle: str | None
    coverage_state: str | None
    rule_id: str | None
    rule_revision: int | None
    produced_by_run: uuid.UUID
    owner_id: str | None
    task_arn: str | None


@dataclass(frozen=True, slots=True)
class ContentBlobRefRow:
    blob_digest: Digest
    bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ValidatedObservation:
    observation_id: uuid.UUID
    trace_digest: Digest
    semantic_work_key: Digest
    input_resource_set_digest: Digest
    coverage_state: str
    spec_id: uuid.UUID


def commit_microchunk(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    generation: int,
    microchunk_key: str,
    input_slice_digest: Digest,
    blob: BlobPublication,
    run_id: uuid.UUID,
    fence: int,
    owner_id: str,
) -> MicrochunkOutcome:
    """Lock generation + claim; verify live fence; insert fragment_commits.

    ON CONFLICT accepts only a byte-identical ``(input_slice_digest, blob_digest)``
    tuple. A different blob for the same microchunk key is nondeterminism.
    """

    def _tx(cur: psycopg.Cursor) -> MicrochunkOutcome:
        cur.execute(
            """
            SELECT claim_key, lifecycle_state
              FROM work_generations
             WHERE namespace_id = %s
               AND semantic_work_key = %s
               AND generation = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key, generation),
        )
        gen_row = cur.fetchone()
        if gen_row is None:
            return MicrochunkOutcome.REJECTED_FENCE
        claim_key, lifecycle = gen_row
        if lifecycle == "INVALIDATED":
            return MicrochunkOutcome.REJECTED_FENCE

        cur.execute(
            """
            SELECT owner_id, run_id, fence, state
              FROM work_claims
             WHERE work_key = %s
             FOR UPDATE
            """,
            (claim_key,),
        )
        claim_row = cur.fetchone()
        if claim_row is None:
            return MicrochunkOutcome.REJECTED_FENCE
        live_owner, live_run, live_fence, state = claim_row
        if state not in ("CLAIMED", "RUNNING"):
            return MicrochunkOutcome.REJECTED_FENCE
        if live_owner != owner_id or live_run != run_id or live_fence != fence:
            return MicrochunkOutcome.REJECTED_FENCE

        # Ensure the blob row exists so the FK can resolve. Publication
        # already verified bytes; this is metadata only.
        cur.execute(
            """
            INSERT INTO content_blobs
              (blob_digest, bucket, object_key, version_id, checksum_sha256,
               size_bytes, canonicalization_version, integrity_state)
            VALUES (%s, %s, %s, %s, %s, %s, 'cas/v1', 'VALID')
            ON CONFLICT (blob_digest) DO NOTHING
            """,
            (
                blob.blob_digest,
                blob.bucket,
                blob.object_key,
                blob.version_id,
                blob.checksum_sha256,
                blob.size_bytes,
            ),
        )

        cur.execute(
            """
            SELECT input_slice_digest, blob_digest
              FROM fragment_commits
             WHERE namespace_id = %s
               AND semantic_work_key = %s
               AND generation = %s
               AND microchunk_key = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key, generation, microchunk_key),
        )
        existing = cur.fetchone()
        if existing is not None:
            prev_slice, prev_blob = existing
            if prev_slice == input_slice_digest and prev_blob == blob.blob_digest:
                return MicrochunkOutcome.ALREADY_PRESENT
            return MicrochunkOutcome.NONDETERMINISTIC

        cur.execute(
            """
            INSERT INTO fragment_commits
              (namespace_id, semantic_work_key, generation, microchunk_key,
               input_slice_digest, blob_digest, committed_by_run, committed_fence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                namespace_id,
                semantic_work_key,
                generation,
                microchunk_key,
                input_slice_digest,
                blob.blob_digest,
                run_id,
                fence,
            ),
        )
        return MicrochunkOutcome.COMMITTED

    return in_txn(pool, _tx, op="flight.commit_microchunk")


@dataclass(frozen=True, slots=True)
class CandidatePersistResult:
    observation_id: uuid.UUID
    trace_digest: Digest
    run_id: uuid.UUID
    spec_id: uuid.UUID
    lifecycle_state: str


def ensure_namespace(pool: ConnectionPool, namespace_id: str, display_name: str) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO namespaces (namespace_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (namespace_id) DO NOTHING
            """,
            (namespace_id, display_name),
        )

    in_txn(pool, _tx, op="flight.ensure_namespace")


def persist_candidate_observation(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    spec: ExecutionSpec,
    trace: TraceContent,
    run_id: uuid.UUID,
    region: str,
    task_arn: str | None = None,
    work_key: str | None = None,
) -> CandidatePersistResult:
    """Persist trace_contents + resources + a CANDIDATE/INCOMPLETE observation.

    One SERIALIZABLE transaction after the run. Never publishes a reusable
    derivation — scout is evidence-only (Day 2).
    """
    from hashlib import sha256

    from cairn.fingerprint.canon import canonical_json
    from cairn.flight import identity as flight_identity

    t_digest = flight_identity.trace_digest(trace)
    s_digest = flight_identity.spec_digest(spec)
    c_key = flight_identity.compatibility_key(spec)
    cov_digest = flight_identity.coverage_profile_digest(spec)
    in_digest = flight_identity.input_resource_set_digest(trace)
    out_digest = flight_identity.output_evidence_digest(trace)
    platform_d = flight_identity.platform_digest(spec)
    env_d = sha256(
        canonical_json(
            {
                "label": "environment_names",
                "v": flight_identity.SCHEMA_VERSION,
                "body": list(spec.purity.env_names),
            }
        )
    ).hexdigest()
    out_c = flight_identity.output_contract_digest(spec)
    sem_key = flight_identity.semantic_work_key(
        spec_digest=s_digest,
        platform_digest=platform_d,
        environment_digest=env_d,
        resolved_inputs=trace.inputs,
        adapter=spec.adapter,
        output_contract_digest=out_c,
    )
    observation_id = uuid.uuid4()
    stage = "scout"
    claim_work_key = work_key or f"scout:{s_digest[:16]}"
    # Shadow and complete-looking traces still land as CANDIDATE — scout
    # never publishes a reusable derivation.
    lifecycle = "INCOMPLETE" if trace.coverage_state.is_incomplete else "CANDIDATE"

    def _tx(cur: psycopg.Cursor) -> CandidatePersistResult:
        cur.execute(
            """
            INSERT INTO namespaces (namespace_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (namespace_id) DO NOTHING
            """,
            (namespace_id, namespace_id),
        )
        cur.execute(
            """
            INSERT INTO execution_specs
              (namespace_id, compatibility_key, spec_digest, argv, cwd_rel,
               output_contract, platform_contract, purity_policy,
               coverage_profile_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace_id, spec_digest) DO UPDATE
              SET compatibility_key = EXCLUDED.compatibility_key
            RETURNING spec_id
            """,
            (
                namespace_id,
                c_key,
                s_digest,
                Jsonb(list(spec.argv)),
                spec.cwd_rel,
                Jsonb({"path_rel": spec.output.path_rel, "kind": spec.output.kind}),
                Jsonb(
                    {
                        "os": spec.platform.os,
                        "arch": spec.platform.arch,
                        "image_digest": spec.platform.image_digest,
                    }
                ),
                Jsonb(
                    {
                        "contract_id": spec.purity.contract_id,
                        "network": spec.purity.network,
                        "clock": spec.purity.clock,
                        "random": spec.purity.random,
                        "env_names": list(spec.purity.env_names),
                        "qualification_runs": spec.purity.qualification_runs,
                    }
                ),
                cov_digest,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        spec_id = row[0]

        cur.execute(
            """
            INSERT INTO runs (run_id, work_key, stage, state, region, task_arn)
            VALUES (%s, %s, %s, 'SUCCEEDED', %s, %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (run_id, claim_work_key, stage, region, task_arn),
        )

        cur.execute(
            """
            INSERT INTO trace_contents
              (trace_digest, coverage_profile_digest, input_resource_set_digest,
               output_evidence_digest, coverage_state, incomplete_reasons)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (trace_digest) DO NOTHING
            """,
            (
                t_digest,
                cov_digest,
                in_digest,
                out_digest,
                trace.coverage_state.value,
                Jsonb(list(trace.incomplete_reasons)),
            ),
        )

        for resource in trace.resources:
            cur.execute(
                """
                INSERT INTO trace_resources
                  (trace_digest, resource_kind, resource_ref, access_mode,
                   "exists", version_digest, resolver, observation_source, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    t_digest,
                    resource.kind.value,
                    resource.ref,
                    resource.access_mode.value,
                    resource.exists,
                    resource.version_digest,
                    resource.resolver,
                    resource.source.value,
                    Jsonb(dict(resource.metadata)),
                ),
            )

        cur.execute(
            """
            INSERT INTO trace_observations
              (observation_id, namespace_id, spec_id, trace_digest, run_id,
               semantic_work_key, lifecycle_state)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, spec_id) DO UPDATE
              SET trace_digest = EXCLUDED.trace_digest,
                  lifecycle_state = EXCLUDED.lifecycle_state
            RETURNING observation_id
            """,
            (
                observation_id,
                namespace_id,
                spec_id,
                t_digest,
                run_id,
                sem_key,
                lifecycle,
            ),
        )
        obs_row = cur.fetchone()
        assert obs_row is not None
        return CandidatePersistResult(
            observation_id=obs_row[0],
            trace_digest=t_digest,
            run_id=run_id,
            spec_id=spec_id,
            lifecycle_state=lifecycle,
        )

    return in_txn(pool, _tx, op="flight.persist_candidate_observation")


def _acquire_claim_on_cursor(
    cur: psycopg.Cursor,
    *,
    claim_key: Digest,
    stage: str,
    owner_id: str,
    host: str,
    region: str,
    run_id: uuid.UUID,
) -> GenerationClaim:
    """Fenced acquire inside an already-open SERIALIZABLE txn."""
    cur.execute(
        """
        INSERT INTO work_claims
          (work_key, stage, state, owner_id, owner_host, owner_region,
           fence, lease_expires_at, run_id)
        VALUES (%s,%s,'CLAIMED',%s,%s,%s,1, now() + %s, %s)
        ON CONFLICT (work_key) DO NOTHING
        RETURNING fence
        """,
        (claim_key, stage, owner_id, host, region, f"{LEASE_SECONDS} seconds", run_id),
    )
    row = cur.fetchone()
    if row:
        return GenerationClaim(
            role=ClaimRole.OWNER,
            generation=0,  # caller fills
            claim_key=claim_key,
            fence=row[0],
            owner_id=owner_id,
            owner_host=host,
            owner_region=region,
            owner_run_id=run_id,
        )

    cur.execute(
        """
        SELECT state, owner_id, owner_host, owner_region, fence,
               lease_expires_at, artifact_id, derivation_id, run_id
          FROM work_claims WHERE work_key=%s FOR UPDATE
        """,
        (claim_key,),
    )
    found = cur.fetchone()
    assert found is not None
    st, o, oh, orr, fence, exp, _art, der, rid = found

    if st == "SUCCEEDED" and der is not None:
        cur.execute(
            "SELECT blob_digest FROM derivations WHERE derivation_id = %s",
            (der,),
        )
        blob_row = cur.fetchone()
        return GenerationClaim(
            role=ClaimRole.RESTORE,
            generation=0,
            claim_key=claim_key,
            fence=fence,
            owner_id=o,
            owner_host=oh,
            owner_region=orr,
            owner_run_id=rid,
            derivation_id=der,
            blob_digest=blob_row[0] if blob_row else None,
        )

    cur.execute("SELECT now()")
    now_ts = cur.fetchone()
    assert now_ts is not None
    is_terminal = st in ("FAILED", "ABANDONED", "INVALIDATED")
    if is_terminal or exp < now_ts[0]:
        cur.execute(
            """
            UPDATE work_claims
               SET state='CLAIMED', owner_id=%s, owner_host=%s, owner_region=%s,
                   fence=fence+1, lease_expires_at=now() + %s,
                   run_id=%s, artifact_id=NULL, derivation_id=NULL, updated_at=now()
             WHERE work_key=%s
         RETURNING fence
            """,
            (owner_id, host, region, f"{LEASE_SECONDS} seconds", run_id, claim_key),
        )
        new_row = cur.fetchone()
        assert new_row is not None
        new_fence = new_row[0]
        cur.execute(
            """
            INSERT INTO ownership_transfers
              (work_key, from_owner, to_owner, from_fence, to_fence, reason)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                claim_key,
                o,
                owner_id,
                fence,
                new_fence,
                "prior_terminal" if is_terminal else "lease_expired",
            ),
        )
        return GenerationClaim(
            role=ClaimRole.OWNER,
            generation=0,
            claim_key=claim_key,
            fence=new_fence,
            took_over_from=o,
            owner_id=owner_id,
            owner_host=host,
            owner_region=region,
            owner_run_id=run_id,
        )

    return GenerationClaim(
        role=ClaimRole.SUBSCRIBER,
        generation=0,
        claim_key=claim_key,
        fence=fence,
        owner_id=o,
        owner_host=oh,
        owner_region=orr,
        owner_run_id=rid,
    )


def open_generation(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    stage_label: str,
    owner_id: str,
    host: str,
    region: str,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
) -> GenerationClaim:
    """Read work head, create generation 1 if absent, acquire/join/takeover."""

    def _tx(cur: psycopg.Cursor) -> GenerationClaim:
        cur.execute(
            """
            INSERT INTO namespaces (namespace_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (namespace_id) DO NOTHING
            """,
            (namespace_id, namespace_id),
        )
        cur.execute(
            """
            SELECT current_generation FROM work_heads
             WHERE namespace_id = %s AND semantic_work_key = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key),
        )
        head = cur.fetchone()
        if head is None:
            generation = 1
            c_key = flight_identity.claim_key(namespace_id, semantic_work_key, generation)
            # Claim row first — work_generations.claim_key FK references it.
            claim = _acquire_claim_on_cursor(
                cur,
                claim_key=c_key,
                stage=stage_label,
                owner_id=owner_id,
                host=host,
                region=region,
                run_id=run_id,
            )
            cur.execute(
                """
                INSERT INTO work_generations
                  (namespace_id, semantic_work_key, generation, claim_key, lifecycle_state)
                VALUES (%s, %s, %s, %s, 'PENDING')
                """,
                (namespace_id, semantic_work_key, generation, c_key),
            )
            cur.execute(
                """
                INSERT INTO work_heads (namespace_id, semantic_work_key, current_generation)
                VALUES (%s, %s, %s)
                """,
                (namespace_id, semantic_work_key, generation),
            )
        else:
            generation = int(head[0])
            cur.execute(
                """
                SELECT claim_key, lifecycle_state, current_derivation_id
                  FROM work_generations
                 WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
                 FOR UPDATE
                """,
                (namespace_id, semantic_work_key, generation),
            )
            gen_row = cur.fetchone()
            assert gen_row is not None
            c_key, lifecycle, cur_der = gen_row
            if lifecycle == "PUBLISHED" and cur_der is not None:
                cur.execute(
                    "SELECT blob_digest FROM derivations WHERE derivation_id = %s",
                    (cur_der,),
                )
                blob_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT owner_id, owner_host, owner_region, fence, run_id
                      FROM work_claims WHERE work_key = %s
                    """,
                    (c_key,),
                )
                claim_row = cur.fetchone()
                return GenerationClaim(
                    role=ClaimRole.RESTORE,
                    generation=generation,
                    claim_key=c_key,
                    fence=claim_row[3] if claim_row else None,
                    owner_id=claim_row[0] if claim_row else None,
                    owner_host=claim_row[1] if claim_row else None,
                    owner_region=claim_row[2] if claim_row else None,
                    owner_run_id=claim_row[4] if claim_row else None,
                    derivation_id=cur_der,
                    blob_digest=blob_row[0] if blob_row else None,
                )
            claim = _acquire_claim_on_cursor(
                cur,
                claim_key=c_key,
                stage=stage_label,
                owner_id=owner_id,
                host=host,
                region=region,
                run_id=run_id,
            )

        cur.execute(
            """
            INSERT INTO runs (run_id, work_key, stage, state, region)
            VALUES (%s, %s, %s, 'RUNNING', %s)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (run_id, claim.claim_key, stage_label, region),
        )

        # Owner's initiating request is also an interest so last-cancel works.
        cur.execute(
            """
            INSERT INTO work_subscribers
              (namespace_id, semantic_work_key, generation, subscriber_id,
               request_id, run_id, joined_fence, state, lease_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'LIVE', now() + %s)
            ON CONFLICT (namespace_id, semantic_work_key, generation, request_id)
            DO NOTHING
            """,
            (
                namespace_id,
                semantic_work_key,
                generation,
                request_id,
                request_id,
                run_id,
                claim.fence or 1,
                f"{LEASE_SECONDS} seconds",
            ),
        )
        return GenerationClaim(
            role=claim.role,
            generation=generation,
            claim_key=claim.claim_key,
            fence=claim.fence,
            took_over_from=claim.took_over_from,
            owner_id=claim.owner_id,
            owner_host=claim.owner_host,
            owner_region=claim.owner_region,
            owner_run_id=claim.owner_run_id,
            derivation_id=claim.derivation_id,
            blob_digest=claim.blob_digest,
        )

    result = in_txn(pool, _tx, op="flight.open_generation")
    if result.role is ClaimRole.OWNER and result.took_over_from is not None:
        emit_event(
            "flight.takeover",
            {
                "namespace_id": namespace_id,
                "semantic_work_key": semantic_work_key,
                "generation": result.generation,
                "fence": result.fence,
                "took_over_from": result.took_over_from,
                "owner_id": owner_id,
            },
            run_id=str(run_id),
        )
    elif result.role is ClaimRole.OWNER:
        emit_event(
            "flight.acquired",
            {
                "namespace_id": namespace_id,
                "semantic_work_key": semantic_work_key,
                "generation": result.generation,
                "fence": result.fence,
                "owner_id": owner_id,
            },
            run_id=str(run_id),
        )
    elif result.role is ClaimRole.SUBSCRIBER:
        emit_event(
            "flight.subscribed",
            {
                "namespace_id": namespace_id,
                "semantic_work_key": semantic_work_key,
                "generation": result.generation,
                "owner_id": result.owner_id,
                "fence": result.fence,
            },
            run_id=str(run_id),
        )
    return result


def register_interest(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    generation: int,
    subscriber_id: uuid.UUID,
    request_id: uuid.UUID,
    run_id: uuid.UUID,
    joined_fence: int,
) -> InterestOutcome:
    """Idempotent on (namespace, key, generation, request_id)."""

    def _tx(cur: psycopg.Cursor) -> InterestOutcome:
        cur.execute(
            """
            INSERT INTO work_subscribers
              (namespace_id, semantic_work_key, generation, subscriber_id,
               request_id, run_id, joined_fence, state, lease_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'LIVE', now() + %s)
            ON CONFLICT (namespace_id, semantic_work_key, generation, request_id)
            DO NOTHING
            RETURNING subscriber_id
            """,
            (
                namespace_id,
                semantic_work_key,
                generation,
                subscriber_id,
                request_id,
                run_id,
                joined_fence,
                f"{LEASE_SECONDS} seconds",
            ),
        )
        row = cur.fetchone()
        return InterestOutcome.REGISTERED if row else InterestOutcome.ALREADY_PRESENT

    return in_txn(pool, _tx, op="flight.register_interest")


def detach_interest(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    generation: int,
    subscriber_id: uuid.UUID,
) -> InterestOutcome:
    def _tx(cur: psycopg.Cursor) -> InterestOutcome:
        cur.execute(
            """
            SELECT state FROM work_subscribers
             WHERE namespace_id = %s AND semantic_work_key = %s
               AND generation = %s AND subscriber_id = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key, generation, subscriber_id),
        )
        row = cur.fetchone()
        if row is None:
            return InterestOutcome.NOT_FOUND
        if row[0] != "LIVE":
            return InterestOutcome.ALREADY_DETACHED
        cur.execute(
            """
            UPDATE work_subscribers
               SET state='DETACHED', detached_at=now()
             WHERE namespace_id = %s AND semantic_work_key = %s
               AND generation = %s AND subscriber_id = %s
            """,
            (namespace_id, semantic_work_key, generation, subscriber_id),
        )
        cur.execute(
            """
            SELECT COUNT(*) FROM work_subscribers
             WHERE namespace_id = %s AND semantic_work_key = %s
               AND generation = %s AND state = 'LIVE'
            """,
            (namespace_id, semantic_work_key, generation),
        )
        remaining = cur.fetchone()
        assert remaining is not None
        if remaining[0] == 0:
            return InterestOutcome.LAST_INTEREST
        return InterestOutcome.DETACHED

    return in_txn(pool, _tx, op="flight.detach_interest")


def select_validated_observations(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    compatibility_key: Digest,
    limit: int = 8,
) -> list[ValidatedObservation]:
    """Bounded compatible VALIDATED observations (§20 selector)."""

    def _tx(cur: psycopg.Cursor) -> list[ValidatedObservation]:
        cur.execute(
            """
            SELECT o.observation_id, o.trace_digest, o.semantic_work_key,
                   t.input_resource_set_digest, t.coverage_state, o.spec_id
              FROM execution_specs AS s
              JOIN trace_observations AS o
                ON o.spec_id = s.spec_id AND o.namespace_id = s.namespace_id
              JOIN trace_contents AS t ON t.trace_digest = o.trace_digest
             WHERE s.namespace_id = %s
               AND s.compatibility_key = %s
               AND o.lifecycle_state = 'VALIDATED'
             ORDER BY o.observed_at DESC, o.observation_id DESC
             LIMIT %s
            """,
            (namespace_id, compatibility_key, limit),
        )
        return [
            ValidatedObservation(
                observation_id=r[0],
                trace_digest=r[1],
                semantic_work_key=r[2],
                input_resource_set_digest=r[3],
                coverage_state=r[4],
                spec_id=r[5],
            )
            for r in cur.fetchall()
        ]

    return in_txn(pool, _tx, op="flight.select_validated_observations")


def select_candidate_observations(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    compatibility_key: Digest,
    limit: int = 8,
) -> list[ValidatedObservation]:
    """Bounded compatible CANDIDATE observations (learning selector)."""

    def _tx(cur: psycopg.Cursor) -> list[ValidatedObservation]:
        cur.execute(
            """
            SELECT o.observation_id, o.trace_digest, o.semantic_work_key,
                   t.input_resource_set_digest, t.coverage_state, o.spec_id
              FROM execution_specs AS s
              JOIN trace_observations AS o
                ON o.spec_id = s.spec_id AND o.namespace_id = s.namespace_id
              JOIN trace_contents AS t ON t.trace_digest = o.trace_digest
             WHERE s.namespace_id = %s
               AND s.compatibility_key = %s
               AND o.lifecycle_state = 'CANDIDATE'
             ORDER BY o.observed_at DESC, o.observation_id DESC
             LIMIT %s
            """,
            (namespace_id, compatibility_key, limit),
        )
        return [
            ValidatedObservation(
                observation_id=r[0],
                trace_digest=r[1],
                semantic_work_key=r[2],
                input_resource_set_digest=r[3],
                coverage_state=r[4],
                spec_id=r[5],
            )
            for r in cur.fetchall()
        ]

    return in_txn(pool, _tx, op="flight.select_candidate_observations")


def promote_observation(
    pool: ConnectionPool,
    *,
    observation_id: uuid.UUID,
    validator_run_id: uuid.UUID,
    namespace_id: str,
) -> bool:
    """CANDIDATE → VALIDATED when a second qualification run matches."""

    def _tx(cur: psycopg.Cursor) -> bool:
        # Ensure validator run exists for the FK.
        cur.execute("SELECT 1 FROM runs WHERE run_id = %s", (validator_run_id,))
        if cur.fetchone() is None:
            cur.execute(
                """
                INSERT INTO runs (run_id, work_key, stage, state, region)
                VALUES (%s, %s, 'exec', 'SUCCEEDED', 'local')
                ON CONFLICT (run_id) DO NOTHING
                """,
                (validator_run_id, f"qualify:{observation_id}"),
            )
        cur.execute(
            """
            UPDATE trace_observations
               SET lifecycle_state = 'VALIDATED',
                   validated_by_run_id = %s
             WHERE observation_id = %s
               AND namespace_id = %s
               AND lifecycle_state = 'CANDIDATE'
            """,
            (validator_run_id, observation_id, namespace_id),
        )
        return cur.rowcount == 1

    return in_txn(pool, _tx, op="flight.promote_observation")


def supersede_observation(
    pool: ConnectionPool,
    *,
    observation_id: uuid.UUID,
    namespace_id: str,
    reason: str,
) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            UPDATE trace_observations
               SET lifecycle_state = 'SUPERSEDED'
             WHERE observation_id = %s
               AND namespace_id = %s
               AND lifecycle_state IN ('CANDIDATE', 'VALIDATED')
            """,
            (observation_id, namespace_id),
        )
        # reason is audit-only; no S3/logging side effects inside the txn.
        _ = reason

    in_txn(pool, _tx, op="flight.supersede_observation")


def publish_derivation(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    generation: int,
    derivation_id: uuid.UUID,
    publication_operation_id: uuid.UUID,
    blob: BlobPublication,
    observation_id: uuid.UUID | None,
    authority: Authority,
    run_id: uuid.UUID,
    owner_id: str,
    fence: int,
    rule: RuleRevisionRef | None = None,
    leaves: Sequence[LeafEdge] = (),
    stage_label: str = "exec",
    decision_explanation: str = "published whole-result derivation",
) -> PublishOutcome:
    """Steps 5–8 of the §12 publish protocol — sole reachability writer."""

    def _tx(cur: psycopg.Cursor) -> PublishOutcome:
        cur.execute(
            """
            SELECT claim_key, lifecycle_state, current_derivation_id,
                   publication_operation_id
              FROM work_generations
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key, generation),
        )
        gen = cur.fetchone()
        if gen is None:
            return PublishOutcome.REJECTED_FENCE
        claim_key, lifecycle, cur_der, prior_pub = gen
        if prior_pub == publication_operation_id and cur_der is not None:
            return PublishOutcome.ALREADY_PRESENT
        if lifecycle == "INVALIDATED":
            return PublishOutcome.REJECTED_FENCE

        cur.execute(
            """
            SELECT owner_id, run_id, fence, state
              FROM work_claims WHERE work_key = %s FOR UPDATE
            """,
            (claim_key,),
        )
        claim = cur.fetchone()
        if claim is None:
            return PublishOutcome.REJECTED_FENCE
        live_owner, live_run, live_fence, state = claim
        if state not in ("CLAIMED", "RUNNING"):
            return PublishOutcome.REJECTED_FENCE
        if live_owner != owner_id or live_run != run_id or live_fence != fence:
            return PublishOutcome.REJECTED_FENCE

        obs_lifecycle: str | None = None
        if observation_id is not None:
            cur.execute(
                """
                SELECT lifecycle_state, semantic_work_key
                  FROM trace_observations
                 WHERE observation_id = %s AND namespace_id = %s
                 FOR UPDATE
                """,
                (observation_id, namespace_id),
            )
            obs = cur.fetchone()
            if obs is None:
                return PublishOutcome.STALE_AUTHORITY
            obs_lifecycle, obs_key = obs
            if obs_key != semantic_work_key:
                return PublishOutcome.STALE_AUTHORITY
            if obs_lifecycle in ("INVALIDATED", "SUPERSEDED", "INCOMPLETE"):
                return PublishOutcome.STALE_AUTHORITY
            # Identity reuse requires VALIDATED; CANDIDATE may publish for
            # learning reachability but cannot authorize restore (selector).
            if authority is Authority.IDENTITY and obs_lifecycle not in (
                "CANDIDATE",
                "VALIDATED",
            ):
                return PublishOutcome.STALE_AUTHORITY

        if rule is not None:
            cur.execute(
                """
                SELECT rh.current_revision, rr.state
                  FROM reuse_rule_heads AS rh
                  JOIN reuse_rule_revisions AS rr
                    ON rr.rule_id = rh.rule_id AND rr.revision = rh.current_revision
                 WHERE rh.rule_id = %s
                 FOR UPDATE OF rh
                """,
                (rule.rule_id,),
            )
            head = cur.fetchone()
            if (
                head is None
                or head[0] != rule.revision
                or head[1]
                not in (
                    "ACTIVE",
                    "TIGHTENED",
                )
            ):
                return PublishOutcome.STALE_AUTHORITY

        for leaf in leaves:
            cur.execute(
                """
                SELECT state, quarantined_at, generation
                  FROM derivations
                 WHERE derivation_id = %s AND namespace_id = %s
                 FOR UPDATE
                """,
                (leaf.child_derivation_id, namespace_id),
            )
            child = cur.fetchone()
            if child is None or child[0] != "PUBLISHED" or child[1] is not None:
                return PublishOutcome.STALE_AUTHORITY

        cur.execute(
            """
            INSERT INTO content_blobs
              (blob_digest, bucket, object_key, version_id, checksum_sha256,
               size_bytes, canonicalization_version, integrity_state)
            VALUES (%s, %s, %s, %s, %s, %s, 'cas/v1', 'VALID')
            ON CONFLICT (blob_digest) DO NOTHING
            """,
            (
                blob.blob_digest,
                blob.bucket,
                blob.object_key,
                blob.version_id,
                blob.checksum_sha256,
                blob.size_bytes,
            ),
        )

        cur.execute(
            """
            INSERT INTO derivations
              (derivation_id, namespace_id, semantic_work_key, generation,
               blob_digest, observation_id, produced_by_run, committed_fence,
               rule_id, rule_revision, state)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PUBLISHED')
            ON CONFLICT (namespace_id, semantic_work_key, generation) DO NOTHING
            RETURNING derivation_id
            """,
            (
                derivation_id,
                namespace_id,
                semantic_work_key,
                generation,
                blob.blob_digest,
                observation_id,
                run_id,
                fence,
                rule.rule_id if rule else None,
                rule.revision if rule else None,
            ),
        )
        inserted = cur.fetchone()
        if inserted is None:
            # Ambiguous-commit reconciliation: accept only identical tuple.
            cur.execute(
                """
                SELECT derivation_id, blob_digest, publication_operation_id
                  FROM work_generations g
                  JOIN derivations d ON d.derivation_id = g.current_derivation_id
                 WHERE g.namespace_id = %s AND g.semantic_work_key = %s
                   AND g.generation = %s
                """,
                (namespace_id, semantic_work_key, generation),
            )
            existing = cur.fetchone()
            if existing and existing[0] == derivation_id and existing[1] == blob.blob_digest:
                return PublishOutcome.ALREADY_PRESENT
            if prior_pub == publication_operation_id:
                return PublishOutcome.ALREADY_PRESENT
            return PublishOutcome.CONFLICT

        for leaf in leaves:
            cur.execute(
                """
                INSERT INTO derivation_fragments
                  (namespace_id, parent_derivation_id, partition_key, ordinal,
                   child_derivation_id, input_slice_digest)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    namespace_id,
                    derivation_id,
                    leaf.partition_key,
                    leaf.ordinal,
                    leaf.child_derivation_id,
                    leaf.input_slice_digest,
                ),
            )

        cur.execute(
            """
            UPDATE work_generations
               SET lifecycle_state = 'PUBLISHED',
                   current_derivation_id = %s,
                   publication_operation_id = %s,
                   updated_at = now()
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
            """,
            (
                derivation_id,
                publication_operation_id,
                namespace_id,
                semantic_work_key,
                generation,
            ),
        )

        cur.execute(
            """
            UPDATE work_claims
               SET state = 'SUCCEEDED',
                   derivation_id = %s,
                   artifact_id = NULL,
                   updated_at = now()
             WHERE work_key = %s AND owner_id = %s AND fence = %s
               AND state IN ('CLAIMED', 'RUNNING')
            """,
            (derivation_id, claim_key, owner_id, fence),
        )
        if cur.rowcount != 1:
            return PublishOutcome.REJECTED_FENCE

        cur.execute(
            """
            UPDATE work_subscribers
               SET state = 'COMPLETED'
             WHERE namespace_id = %s AND semantic_work_key = %s
               AND generation = %s AND state = 'LIVE'
            """,
            (namespace_id, semantic_work_key, generation),
        )

        # Wire the decision to observation / derivation / rule revision.
        authorized = authority.value if obs_lifecycle == "VALIDATED" else None
        verdict = "reuse" if authorized else "recompute"
        action = "REUSE" if authorized else "RECOMPUTE"
        cur.execute(
            """
            INSERT INTO reuse_decisions
              (decision_id, work_key, stage, action, verdict, change_class,
               proposed_by, model_id, authorized_by, probe_run_id,
               candidate_artifact_id, latency_ms, explanation,
               observation_id, derivation_id, rule_id, rule_revision)
            VALUES (%s,%s,%s,%s,%s,NULL,'rule',NULL,%s,NULL,NULL,0,%s,%s,%s,%s,%s)
            """,
            (
                uuid.uuid4(),
                claim_key,
                stage_label,
                action,
                verdict,
                authorized,
                decision_explanation,
                observation_id,
                derivation_id,
                rule.rule_id if rule else None,
                rule.revision if rule else None,
            ),
        )
        return PublishOutcome.PUBLISHED

    outcome = in_txn(pool, _tx, op="flight.publish_derivation")
    emit_event(
        "flight.publish_derivation",
        {
            "outcome": outcome.value,
            "namespace_id": namespace_id,
            "semantic_work_key": semantic_work_key,
            "generation": generation,
            "derivation_id": str(derivation_id),
            "blob_digest": blob.blob_digest,
            "authority": authority.value,
        },
        run_id=str(run_id),
    )
    return outcome


def current_derivations(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    keys: Sequence[Digest],
) -> dict[Digest, CurrentDerivation]:
    """Batched §20 selector: PUBLISHED + VALID blob + VALIDATED observation."""
    if not keys:
        return {}

    def _tx(cur: psycopg.Cursor) -> dict[Digest, CurrentDerivation]:
        cur.execute(
            """
            SELECT h.semantic_work_key, h.current_generation,
                   d.derivation_id, d.blob_digest,
                   b.bucket, b.object_key, b.version_id, b.checksum_sha256, b.size_bytes,
                   o.observation_id, o.lifecycle_state, t.coverage_state,
                   d.rule_id, d.rule_revision, d.produced_by_run,
                   wc.owner_id, r.task_arn
              FROM work_heads AS h
              JOIN work_generations AS g
                ON g.namespace_id = h.namespace_id
               AND g.semantic_work_key = h.semantic_work_key
               AND g.generation = h.current_generation
              JOIN derivations AS d ON d.derivation_id = g.current_derivation_id
              JOIN content_blobs AS b ON b.blob_digest = d.blob_digest
              -- LEFT, not JOIN: a leaf/fragment derivation under a declared
              -- adapter contract (jsonl-map/v1) has no trace observation at
              -- all — its authority is the adapter contract, not a trace
              -- (Authority.STRUCTURAL). An INNER join here made every such
              -- derivation permanently unreachable by this selector, so a
              -- leaf could never be found "current" and restored.
              LEFT JOIN trace_observations AS o
                ON o.observation_id = d.observation_id AND o.namespace_id = d.namespace_id
              LEFT JOIN trace_contents AS t ON t.trace_digest = o.trace_digest
              LEFT JOIN reuse_rule_heads AS rh ON rh.rule_id = d.rule_id
              LEFT JOIN reuse_rule_revisions AS rr
                ON rr.rule_id = d.rule_id AND rr.revision = d.rule_revision
              LEFT JOIN work_claims AS wc ON wc.work_key = g.claim_key
              LEFT JOIN runs AS r ON r.run_id = d.produced_by_run
             WHERE h.namespace_id = %s
               AND h.semantic_work_key = ANY(%s)
               AND g.lifecycle_state = 'PUBLISHED'
               AND d.state = 'PUBLISHED'
               AND d.quarantined_at IS NULL
               AND b.integrity_state = 'VALID'
               AND (d.observation_id IS NULL OR o.lifecycle_state = 'VALIDATED')
               AND (
                 d.rule_id IS NULL
                 OR (
                   rh.current_revision = d.rule_revision
                   AND rr.state IN ('ACTIVE', 'TIGHTENED')
                 )
               )
            """,
            (namespace_id, list(keys)),
        )
        out: dict[Digest, CurrentDerivation] = {}
        for row in cur.fetchall():
            out[row[0]] = CurrentDerivation(
                semantic_work_key=row[0],
                generation=row[1],
                derivation_id=row[2],
                blob_digest=row[3],
                bucket=row[4],
                object_key=row[5],
                version_id=row[6],
                checksum_sha256=row[7],
                size_bytes=row[8],
                observation_id=row[9],
                observation_lifecycle=row[10],
                coverage_state=row[11],
                rule_id=row[12],
                rule_revision=row[13],
                produced_by_run=row[14],
                owner_id=row[15],
                task_arn=row[16],
            )
        return out

    return in_txn(pool, _tx, op="flight.current_derivations")


def invalidate_generation(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    reason: str,
    mark_blob_invalid: Digest | None = None,
) -> int:
    """Mark generation INVALIDATED, bump fence, advance head, return new gen."""

    def _tx(cur: psycopg.Cursor) -> int:
        if mark_blob_invalid is not None:
            cur.execute(
                """
                UPDATE content_blobs
                   SET integrity_state = 'INVALID', quarantined_at = now()
                 WHERE blob_digest = %s AND integrity_state = 'VALID'
                """,
                (mark_blob_invalid,),
            )
        cur.execute(
            """
            SELECT current_generation FROM work_heads
             WHERE namespace_id = %s AND semantic_work_key = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key),
        )
        head = cur.fetchone()
        if head is None:
            raise LookupError(f"no work head for {semantic_work_key}")
        old_gen = int(head[0])
        cur.execute(
            """
            SELECT claim_key, current_derivation_id FROM work_generations
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
             FOR UPDATE
            """,
            (namespace_id, semantic_work_key, old_gen),
        )
        gen_row = cur.fetchone()
        assert gen_row is not None
        claim_key, cur_der = gen_row
        if cur_der is not None:
            cur.execute(
                """
                UPDATE derivations
                   SET state = 'QUARANTINED', quarantined_at = now()
                 WHERE derivation_id = %s AND state = 'PUBLISHED'
                """,
                (cur_der,),
            )
        cur.execute(
            """
            UPDATE work_generations
               SET lifecycle_state = 'INVALIDATED',
                   terminal_reason = %s,
                   updated_at = now()
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
            """,
            (reason, namespace_id, semantic_work_key, old_gen),
        )
        cur.execute(
            """
            UPDATE work_claims
               SET state = 'INVALIDATED', fence = fence + 1, updated_at = now()
             WHERE work_key = %s
         RETURNING fence, owner_id
            """,
            (claim_key,),
        )
        _ = cur.fetchone()

        new_gen = old_gen + 1
        new_claim = flight_identity.claim_key(namespace_id, semantic_work_key, new_gen)
        # Placeholder claim so the generation FK can resolve; next open_generation
        # will acquire for real work.
        cur.execute(
            """
            INSERT INTO work_claims
              (work_key, stage, state, owner_id, owner_host, owner_region,
               fence, lease_expires_at, run_id)
            VALUES (%s,'exec','FAILED','invalidated','invalidated','invalidated',
                    1, now(), %s)
            ON CONFLICT (work_key) DO NOTHING
            """,
            (new_claim, uuid.uuid4()),
        )
        cur.execute(
            """
            INSERT INTO work_generations
              (namespace_id, semantic_work_key, generation, claim_key, lifecycle_state,
               terminal_reason)
            VALUES (%s,%s,%s,%s,'PENDING',NULL)
            """,
            (namespace_id, semantic_work_key, new_gen, new_claim),
        )
        cur.execute(
            """
            UPDATE work_heads
               SET current_generation = %s, updated_at = now()
             WHERE namespace_id = %s AND semantic_work_key = %s
            """,
            (new_gen, namespace_id, semantic_work_key),
        )
        return new_gen

    new_generation = in_txn(pool, _tx, op="flight.invalidate_generation")
    emit_event(
        "flight.invalidate_generation",
        {
            "namespace_id": namespace_id,
            "semantic_work_key": semantic_work_key,
            "new_generation": new_generation,
            "reason": reason,
            "mark_blob_invalid": mark_blob_invalid,
        },
    )
    return new_generation


def find_candidate_observation(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
) -> tuple[uuid.UUID, Digest, str] | None:
    """Newest CANDIDATE for this semantic key, if any."""

    def _tx(cur: psycopg.Cursor) -> tuple[uuid.UUID, Digest, str] | None:
        cur.execute(
            """
            SELECT o.observation_id, o.trace_digest, t.coverage_state
              FROM trace_observations AS o
              JOIN trace_contents AS t ON t.trace_digest = o.trace_digest
             WHERE o.namespace_id = %s
               AND o.semantic_work_key = %s
               AND o.lifecycle_state = 'CANDIDATE'
             ORDER BY o.observed_at DESC
             LIMIT 1
            """,
            (namespace_id, semantic_work_key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    return in_txn(pool, _tx, op="flight.find_candidate_observation")


def list_fragment_commits(
    pool: ConnectionPool,
    *,
    namespace_id: str,
    semantic_work_key: Digest,
    generation: int,
) -> list[tuple[str, Digest, Digest]]:
    """(microchunk_key, input_slice_digest, blob_digest) for one leaf
    generation — used to build a resumed leaf's ``CAIRN_RESUME_MANIFEST``
    and to verify every expected microchunk is durable before leaf publish."""

    def _tx(cur: psycopg.Cursor) -> list[tuple[str, Digest, Digest]]:
        cur.execute(
            """
            SELECT microchunk_key, input_slice_digest, blob_digest
              FROM fragment_commits
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
            """,
            (namespace_id, semantic_work_key, generation),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    return in_txn(pool, _tx, op="flight.list_fragment_commits")


def get_content_blob(pool: ConnectionPool, *, blob_digest: Digest) -> ContentBlobRefRow | None:
    def _tx(cur: psycopg.Cursor) -> ContentBlobRefRow | None:
        cur.execute(
            """
            SELECT blob_digest, bucket, object_key, version_id, checksum_sha256, size_bytes
              FROM content_blobs WHERE blob_digest = %s AND integrity_state = 'VALID'
            """,
            (blob_digest,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ContentBlobRefRow(
            blob_digest=row[0],
            bucket=row[1],
            object_key=row[2],
            version_id=row[3],
            checksum_sha256=row[4],
            size_bytes=row[5],
        )

    return in_txn(pool, _tx, op="flight.get_content_blob")


def record_composite_derivation(
    pool: ConnectionPool,
    *,
    parent_derivation_id: uuid.UUID,
    adapter_id: str,
    partitioner_digest: Digest,
    reducer_digest: Digest,
    verifier_digest: Digest,
    merkle_root_digest: Digest,
    leaf_count: int,
    output_metadata: dict[str, object],
) -> None:
    """Descriptive metadata for the leaf-map/explain surface — not an
    authority-bearing table; ``publish_derivation`` already made the root
    reachable before this is called."""

    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO composite_derivations
              (parent_derivation_id, adapter_id, partitioner_digest, reducer_digest,
               verifier_digest, merkle_root_digest, leaf_count, output_metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (parent_derivation_id) DO NOTHING
            """,
            (
                parent_derivation_id,
                adapter_id,
                partitioner_digest,
                reducer_digest,
                verifier_digest,
                merkle_root_digest,
                leaf_count,
                Jsonb(output_metadata),
            ),
        )

    in_txn(pool, _tx, op="flight.record_composite_derivation")


def lookup_observation_resources(
    pool: ConnectionPool,
    *,
    trace_digest: Digest,
) -> list[tuple[str, str, str, bool, Digest, str, str]]:
    """Returns ``(kind, ref, access_mode, exists, version_digest, resolver,
    observation_source)``. ``observation_source`` is part of every
    ResourceIdentity's hashed payload, so a caller that reconstructs
    ResourceIdentity rows from here and substitutes a different source
    (e.g. hardcoding DECLARED) silently changes semantic_work_key even when
    every other field matches — see flight/executor.py's callers."""

    def _tx(cur: psycopg.Cursor) -> list[tuple[str, str, str, bool, Digest, str, str]]:
        cur.execute(
            """
            SELECT resource_kind, resource_ref, access_mode, "exists",
                   version_digest, resolver, observation_source
              FROM trace_resources
             WHERE trace_digest = %s
             ORDER BY resource_kind, resource_ref, access_mode
            """,
            (trace_digest,),
        )
        return [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in cur.fetchall()]

    return in_txn(pool, _tx, op="flight.lookup_observation_resources")
