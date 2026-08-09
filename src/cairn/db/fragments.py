"""Per-fragment checkpoint persistence — PROJECT.md §4.5, `run_fragments`.

A resuming worker looks up what it already has by `(work_key, fragment_index)`
before recomputing anything; content is verified separately against S3 by
digest (`storage/s3.py::get_fragment_verified`), not trusted from this table
alone.

Every write locks the live ``work_claims`` row and verifies
``{owner_id, run_id, fence, state in (CLAIMED, RUNNING)}`` in the same
SERIALIZABLE transaction. A dispossessed owner therefore cannot insert or
overwrite fragment metadata after takeover — the previous blind upsert was
a correctness hole the docstring falsely claimed was already closed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn


class FragmentFenceError(RuntimeError):
    """Caller is not the live fence holder; the write was refused."""


class FragmentNondeterminism(RuntimeError):
    """Same fragment key already holds a different byte identity."""


class FragmentCommitOutcome(StrEnum):
    COMMITTED = "COMMITTED"
    ALREADY_PRESENT = "ALREADY_PRESENT"


@dataclass(frozen=True, slots=True)
class FragmentRecord:
    work_key: str
    fragment_index: int
    run_id: uuid.UUID
    fence: int
    s3_uri: str
    content_digest: str
    duration_ms: int
    created_at: datetime | None = None


def _require_live_fence(
    cur: psycopg.Cursor,
    work_key: str,
    *,
    owner_id: str,
    run_id: uuid.UUID,
    fence: int,
) -> None:
    cur.execute(
        """
        SELECT owner_id, run_id, fence, state
          FROM work_claims
         WHERE work_key = %s
         FOR UPDATE
        """,
        (work_key,),
    )
    row = cur.fetchone()
    if row is None:
        raise FragmentFenceError(f"no claim row for work_key={work_key!r}")
    live_owner, live_run, live_fence, state = row
    if state not in ("CLAIMED", "RUNNING"):
        raise FragmentFenceError(
            f"claim work_key={work_key!r} state={state!r} is not writable"
        )
    if live_owner != owner_id or live_run != run_id or live_fence != fence:
        raise FragmentFenceError(
            f"fence mismatch for work_key={work_key!r}: "
            f"caller=({owner_id!r}, {run_id}, {fence}) "
            f"live=({live_owner!r}, {live_run}, {live_fence})"
        )


def record_fragment(
    pool: ConnectionPool,
    work_key: str,
    fragment_index: int,
    *,
    owner_id: str,
    run_id: uuid.UUID,
    fence: int,
    s3_uri: str,
    content_digest: str,
    duration_ms: int,
) -> FragmentCommitOutcome:
    """Fenced insert into the five-stage ``run_fragments`` projection.

    ON CONFLICT accepts ONLY a byte-identical ``(s3_uri, content_digest)``
    tuple. A different blob for the same key is nondeterminism, not an update
    — the previous ``DO UPDATE`` let a takeover race rewrite reachability.
    """

    def _tx(cur: psycopg.Cursor) -> FragmentCommitOutcome:
        _require_live_fence(
            cur, work_key, owner_id=owner_id, run_id=run_id, fence=fence
        )
        cur.execute(
            """
            SELECT s3_uri, content_digest
              FROM run_fragments
             WHERE work_key = %s AND fragment_index = %s
             FOR UPDATE
            """,
            (work_key, fragment_index),
        )
        existing = cur.fetchone()
        if existing is not None:
            prev_uri, prev_digest = existing
            if prev_uri == s3_uri and prev_digest == content_digest:
                return FragmentCommitOutcome.ALREADY_PRESENT
            raise FragmentNondeterminism(
                f"fragment work_key={work_key!r} index={fragment_index} already "
                f"holds digest={prev_digest!r}; refusing digest={content_digest!r}"
            )
        cur.execute(
            """
            INSERT INTO run_fragments
              (work_key, fragment_index, run_id, fence, s3_uri, content_digest, duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (work_key, fragment_index, run_id, fence, s3_uri, content_digest, duration_ms),
        )
        return FragmentCommitOutcome.COMMITTED

    return in_txn(pool, _tx, op="fragments.record_fragment")


def list_fragments(pool: ConnectionPool, work_key: str) -> list[FragmentRecord]:
    """All recorded fragments for a work_key, in index order — what a
    resuming worker has to decide which indices it can skip recomputing."""

    def _tx(cur: psycopg.Cursor) -> list[FragmentRecord]:
        cur.execute(
            """
            SELECT work_key, fragment_index, run_id, fence, s3_uri, content_digest,
                   duration_ms, created_at
              FROM run_fragments
             WHERE work_key = %s
             ORDER BY fragment_index
            """,
            (work_key,),
        )
        return [FragmentRecord(*row) for row in cur.fetchall()]

    return in_txn(pool, _tx, op="fragments.list_fragments")


def clear_fragments(pool: ConnectionPool, work_key: str) -> None:
    """Delete every fragment row for a work_key — called once a stage
    completes successfully, since a SUCCEEDED artifact makes the partial
    fragments moot and a future RESUME must never mistake a stale fragment
    for progress on a *new* attempt at the same key (e.g. after
    unquarantine)."""

    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute("DELETE FROM run_fragments WHERE work_key = %s RETURNING NOTHING", (work_key,))

    in_txn(pool, _tx, op="fragments.clear_fragments")
