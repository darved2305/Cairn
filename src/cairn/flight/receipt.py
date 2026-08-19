"""Canonical receipt projection — docs/project/PLAN.md §19 Day 6, Appendix B.2/B.6.

``cairn receipt --run <derivation_id> [--verify]`` renders exactly what
``publish_derivation`` committed: every field here is read from
CockroachDB, and ``--verify`` re-fetches each named blob from S3 and
rehashes it rather than trusting the stored digest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.fingerprint.canon import canonical_json
from cairn.storage.cas import (
    BlobIntegrityError,
    BlobUnavailable,
    ContentBlobRef,
    fetch_blob_verified,
)


class ReceiptNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class LeafReceipt:
    bucket: int
    child_derivation_id: uuid.UUID
    blob_digest: str
    input_slice_digest: str
    s3_bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Receipt:
    derivation_id: uuid.UUID
    namespace_id: str
    semantic_work_key: str
    generation: int
    blob_digest: str
    s3_bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int
    state: str
    produced_by_run: uuid.UUID
    committed_fence: int
    rule_id: str | None
    rule_revision: int | None
    observation_id: uuid.UUID | None
    merkle_root_digest: str | None
    leaf_count: int | None
    leaves: tuple[LeafReceipt, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "derivation_id": str(self.derivation_id),
            "namespace_id": self.namespace_id,
            "semantic_work_key": self.semantic_work_key,
            "generation": self.generation,
            "blob_digest": self.blob_digest,
            "s3_bucket": self.s3_bucket,
            "object_key": self.object_key,
            "version_id": self.version_id,
            "checksum_sha256": self.checksum_sha256,
            "size_bytes": self.size_bytes,
            "state": self.state,
            "produced_by_run": str(self.produced_by_run),
            "committed_fence": self.committed_fence,
            "rule_id": self.rule_id,
            "rule_revision": self.rule_revision,
            "observation_id": str(self.observation_id) if self.observation_id else None,
            "merkle_root_digest": self.merkle_root_digest,
            "leaf_count": self.leaf_count,
            "leaves": [
                {
                    "bucket": leaf.bucket,
                    "child_derivation_id": str(leaf.child_derivation_id),
                    "blob_digest": leaf.blob_digest,
                    "input_slice_digest": leaf.input_slice_digest,
                }
                for leaf in self.leaves
            ],
        }

    def canonical_json(self) -> bytes:
        return canonical_json(self.as_dict())


def load_receipt(pool: ConnectionPool, *, derivation_id: uuid.UUID) -> Receipt:
    def _tx(cur: psycopg.Cursor) -> Receipt | None:
        cur.execute(
            """
            SELECT d.derivation_id, d.namespace_id, d.semantic_work_key, d.generation,
                   d.blob_digest, b.bucket, b.object_key, b.version_id, b.checksum_sha256,
                   b.size_bytes, d.state, d.produced_by_run, d.committed_fence,
                   d.rule_id, d.rule_revision, d.observation_id,
                   c.merkle_root_digest, c.leaf_count
              FROM derivations d
              JOIN content_blobs b ON b.blob_digest = d.blob_digest
              LEFT JOIN composite_derivations c ON c.parent_derivation_id = d.derivation_id
             WHERE d.derivation_id = %s
            """,
            (derivation_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None

        cur.execute(
            """
            SELECT f.ordinal, f.child_derivation_id, f.input_slice_digest,
                   cd.blob_digest, cb.bucket, cb.object_key, cb.version_id,
                   cb.checksum_sha256, cb.size_bytes
              FROM derivation_fragments f
              JOIN derivations cd ON cd.derivation_id = f.child_derivation_id
              JOIN content_blobs cb ON cb.blob_digest = cd.blob_digest
             WHERE f.parent_derivation_id = %s
             ORDER BY f.ordinal
            """,
            (derivation_id,),
        )
        leaves = tuple(
            LeafReceipt(
                bucket=lr[0],
                child_derivation_id=lr[1],
                input_slice_digest=lr[2],
                blob_digest=lr[3],
                s3_bucket=lr[4],
                object_key=lr[5],
                version_id=lr[6],
                checksum_sha256=lr[7],
                size_bytes=lr[8],
            )
            for lr in cur.fetchall()
        )
        return Receipt(
            derivation_id=row[0],
            namespace_id=row[1],
            semantic_work_key=row[2],
            generation=row[3],
            blob_digest=row[4],
            s3_bucket=row[5],
            object_key=row[6],
            version_id=row[7],
            checksum_sha256=row[8],
            size_bytes=row[9],
            state=row[10],
            produced_by_run=row[11],
            committed_fence=row[12],
            rule_id=row[13],
            rule_revision=row[14],
            observation_id=row[15],
            merkle_root_digest=row[16],
            leaf_count=row[17],
            leaves=leaves,
        )

    receipt = in_txn(pool, _tx, op="flight.load_receipt")
    if receipt is None:
        raise ReceiptNotFound(f"no derivation with id={derivation_id}")
    return receipt


def verify_receipt(receipt: Receipt) -> list[str]:
    """Re-fetch and rehash every blob this receipt names. Returns problems
    found; an empty list means every digest it claims checked out against S3."""
    problems: list[str] = []

    def _check(
        label: str,
        blob_digest: str,
        s3_bucket: str,
        object_key: str,
        version_id: str,
        checksum_sha256: str,
        size_bytes: int,
    ) -> None:
        ref = ContentBlobRef(
            blob_digest=blob_digest,
            bucket=s3_bucket,
            object_key=object_key,
            version_id=version_id,
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
        )
        try:
            fetch_blob_verified(ref)
        except BlobIntegrityError as exc:
            problems.append(f"{label}: integrity mismatch: {exc}")
        except BlobUnavailable as exc:
            problems.append(f"{label}: unavailable: {exc}")

    _check(
        "root",
        receipt.blob_digest,
        receipt.s3_bucket,
        receipt.object_key,
        receipt.version_id,
        receipt.checksum_sha256,
        receipt.size_bytes,
    )
    for leaf in receipt.leaves:
        _check(
            f"leaf[{leaf.bucket}]",
            leaf.blob_digest,
            leaf.s3_bucket,
            leaf.object_key,
            leaf.version_id,
            leaf.checksum_sha256,
            leaf.size_bytes,
        )
    return problems
