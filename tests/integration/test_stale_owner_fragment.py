"""Adversarial: a dispossessed owner must not publish microchunks after takeover.

Gate (Appendix E Day 1): neither mutable byte overwrite nor a
``fragment_commits`` insert may succeed under a stale fence. Written before
the fence-checked commit primitive so a green suite cannot hide the gap.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from psycopg.types.json import Jsonb

from cairn.db import claims
from cairn.db.fragments import FragmentFenceError, record_fragment
from cairn.db.txn import in_txn
from cairn.storage import cas

pytestmark = pytest.mark.integration

STAGE = "stale_owner_fragment"
REGION = "us-east-1"


def _ensure_test_environment(pool, env_fingerprint: str) -> None:
    def _tx(cur):  # type: ignore[no-untyped-def]
        cur.execute(
            """INSERT INTO environments
                 (env_fingerprint, image_digest, python_version, deps, torch_threads)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (env_fingerprint) DO NOTHING""",
            (env_fingerprint, "sha256:test-fixture", "3.12.0", Jsonb({"fixture": True}), 1),
        )

    in_txn(pool, _tx, op="test.ensure_environment")


def _force_expire(pool, work_key: str) -> None:
    def _tx(cur):  # type: ignore[no-untyped-def]
        cur.execute(
            "UPDATE work_claims SET lease_expires_at = now() - interval '1 second' "
            "WHERE work_key=%s",
            (work_key,),
        )

    in_txn(pool, _tx, op="test.force_expire")


def _seed_generation(
    pool,
    *,
    namespace_id: str,
    semantic_work_key: str,
    generation: int,
    claim_key: str,
) -> None:
    """Minimal generation row so fragment_commits FK checks can fire."""

    def _tx(cur):  # type: ignore[no-untyped-def]
        cur.execute(
            """
            INSERT INTO namespaces (namespace_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (namespace_id) DO NOTHING
            """,
            (namespace_id, "stale-owner-test"),
        )
        cur.execute(
            """
            INSERT INTO work_generations
              (namespace_id, semantic_work_key, generation, claim_key, lifecycle_state)
            VALUES (%s, %s, %s, %s, 'PENDING')
            ON CONFLICT DO NOTHING
            """,
            (namespace_id, semantic_work_key, generation, claim_key),
        )
        cur.execute(
            """
            INSERT INTO work_heads (namespace_id, semantic_work_key, current_generation)
            VALUES (%s, %s, %s)
            ON CONFLICT (namespace_id, semantic_work_key) DO UPDATE
              SET current_generation = EXCLUDED.current_generation
            """,
            (namespace_id, semantic_work_key, generation),
        )

    in_txn(pool, _tx, op="test.seed_generation")


def test_dispossessed_owner_cannot_record_compat_fragment_or_commit_microchunk(pool) -> None:
    from cairn.db.flight import MicrochunkOutcome, commit_microchunk

    bucket = os.environ.get("CAIRN_S3_BUCKET")
    if not bucket:
        pytest.skip("CAIRN_S3_BUCKET not set — CAS overwrite proof needs a real bucket")

    work_key = f"stale-frag-{uuid.uuid4().hex}"
    semantic = hashlib.sha256(work_key.encode()).hexdigest()
    namespace_id = "test-ns"
    generation = 1
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    owner_a = f"owner-a-{uuid.uuid4().hex[:8]}"
    owner_b = f"owner-b-{uuid.uuid4().hex[:8]}"

    def _insert_run(run_id: uuid.UUID, owner: str) -> None:
        def _tx(cur):  # type: ignore[no-untyped-def]
            cur.execute(
                """
                INSERT INTO runs (run_id, work_key, stage, state, region)
                VALUES (%s, %s, %s, 'RUNNING', %s)
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, work_key, STAGE, REGION),
            )

        in_txn(pool, _tx, op="test.insert_run")

    claim_a = claims.acquire(pool, work_key, STAGE, owner_a, "host-a", REGION, run_a)
    assert claim_a.won and claim_a.fence == 1
    _insert_run(run_a, owner_a)

    _seed_generation(
        pool,
        namespace_id=namespace_id,
        semantic_work_key=semantic,
        generation=generation,
        claim_key=work_key,
    )

    bytes_a = b"microchunk-owner-a-v1"
    pub_a = cas.publish_blob(bucket, bytes_a)
    outcome = commit_microchunk(
        pool,
        namespace_id=namespace_id,
        semantic_work_key=semantic,
        generation=generation,
        microchunk_key="leaf-0/chunk-0",
        input_slice_digest=hashlib.sha256(b"slice-a").hexdigest(),
        blob=pub_a,
        run_id=run_a,
        fence=1,
        owner_id=owner_a,
    )
    assert outcome == MicrochunkOutcome.COMMITTED

    # Compat path under the live fence must also accept an identical-tuple rewrite.
    record_fragment(
        pool,
        work_key,
        0,
        owner_id=owner_a,
        run_id=run_a,
        fence=1,
        s3_uri=f"s3://{bucket}/{pub_a.object_key}",
        content_digest=pub_a.blob_digest,
        duration_ms=1,
    )

    _force_expire(pool, work_key)
    claim_b = claims.acquire(pool, work_key, STAGE, owner_b, "host-b", REGION, run_b)
    assert claim_b.won and claim_b.fence == 2
    assert claim_b.took_over_from == owner_a
    _insert_run(run_b, owner_b)

    # Stale owner: run_fragments write must refuse without reading as success.
    with pytest.raises(FragmentFenceError):
        record_fragment(
            pool,
            work_key,
            0,
            owner_id=owner_a,
            run_id=run_a,
            fence=1,
            s3_uri=f"s3://{bucket}/forged",
            content_digest=hashlib.sha256(b"forged").hexdigest(),
            duration_ms=1,
        )

    # Stale owner: fragment_commits insert must refuse.
    bytes_stale = b"microchunk-stale-overwrite"
    pub_stale = cas.publish_blob(bucket, bytes_stale)
    stale_outcome = commit_microchunk(
        pool,
        namespace_id=namespace_id,
        semantic_work_key=semantic,
        generation=generation,
        microchunk_key="leaf-0/chunk-1",
        input_slice_digest=hashlib.sha256(b"slice-stale").hexdigest(),
        blob=pub_stale,
        run_id=run_a,
        fence=1,
        owner_id=owner_a,
    )
    assert stale_outcome == MicrochunkOutcome.REJECTED_FENCE

    # Fetching A's version still yields A's bytes after B's takeover.
    still = cas.fetch_blob_verified(
        cas.ContentBlobRef(
            blob_digest=pub_a.blob_digest,
            bucket=pub_a.bucket,
            object_key=pub_a.object_key,
            version_id=pub_a.version_id,
            checksum_sha256=pub_a.checksum_sha256,
            size_bytes=pub_a.size_bytes,
        )
    )
    assert still == bytes_a

    forged = cas.ContentBlobRef(
        blob_digest=hashlib.sha256(b"not-a").hexdigest(),
        bucket=pub_a.bucket,
        object_key=pub_a.object_key,
        version_id=pub_a.version_id,
        checksum_sha256=pub_a.checksum_sha256,
        size_bytes=pub_a.size_bytes,
    )
    with pytest.raises(cas.BlobIntegrityError):
        cas.fetch_blob_verified(forged)

    # Live owner can still commit under the new fence.
    bytes_b = b"microchunk-owner-b-v1"
    pub_b = cas.publish_blob(bucket, bytes_b)
    live = commit_microchunk(
        pool,
        namespace_id=namespace_id,
        semantic_work_key=semantic,
        generation=generation,
        microchunk_key="leaf-0/chunk-1",
        input_slice_digest=hashlib.sha256(b"slice-b").hexdigest(),
        blob=pub_b,
        run_id=run_b,
        fence=2,
        owner_id=owner_b,
    )
    assert live == MicrochunkOutcome.COMMITTED
