"""Flight Recorder database primitives (Appendix B.4) — Day-1 subset.

Every function is an ``in_txn`` closure: pure in its arguments, no S3 calls,
no event emission. Events fire after the call returns.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.flight.types import Digest, ExecutionSpec, TraceContent
from cairn.storage.cas import BlobPublication


class MicrochunkOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    REJECTED_FENCE = "REJECTED_FENCE"
    NONDETERMINISTIC = "NONDETERMINISTIC"


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

    from psycopg.types.json import Jsonb

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
