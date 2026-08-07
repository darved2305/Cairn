"""Per-fragment checkpoint persistence — PROJECT.md §4.5, `run_fragments`.

A resuming worker looks up what it already has by `(work_key, fragment_index)`
before recomputing anything; content is verified separately against S3 by
digest (`storage/s3.py::get_fragment_verified`), not trusted from this table
alone. Fenced the same way every other write in this codebase is: only the
fence holder's fragment rows are ever written or trusted for resume.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn


@dataclass(frozen=True)
class FragmentRecord:
    work_key: str
    fragment_index: int
    run_id: uuid.UUID
    fence: int
    s3_uri: str
    content_digest: str
    duration_ms: int
    created_at: datetime | None = None


def record_fragment(
    pool: ConnectionPool,
    work_key: str,
    fragment_index: int,
    *,
    run_id: uuid.UUID,
    fence: int,
    s3_uri: str,
    content_digest: str,
    duration_ms: int,
) -> None:
    """Upsert one fragment row. `ON CONFLICT DO UPDATE` (not `DO NOTHING`):
    a resumed run re-producing the same fragment index legitimately
    overwrites the previous attempt's row with its own run_id/fence/s3_uri,
    the same way `claims.acquire`'s takeover replaces the prior owner."""

    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO run_fragments
              (work_key, fragment_index, run_id, fence, s3_uri, content_digest, duration_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (work_key, fragment_index) DO UPDATE SET
              run_id=excluded.run_id, fence=excluded.fence, s3_uri=excluded.s3_uri,
              content_digest=excluded.content_digest, duration_ms=excluded.duration_ms,
              created_at=now()
            """,
            (work_key, fragment_index, run_id, fence, s3_uri, content_digest, duration_ms),
        )

    in_txn(pool, _tx, op="fragments.record_fragment")


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
