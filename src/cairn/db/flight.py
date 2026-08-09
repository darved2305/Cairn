"""Flight Recorder database primitives (Appendix B.4) — Day-1 subset.

Every function is an ``in_txn`` closure: pure in its arguments, no S3 calls,
no event emission. Events fire after the call returns.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.flight.types import Digest
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
