"""The distributed claim protocol. See PROJECT.md §4.2 for the full design.

Every function here runs through `db.txn.in_txn`, so every write is a
single SERIALIZABLE transaction, replayed whole on 40001. That's what
makes the win/loss decision in `acquire` and the liveness check in
`heartbeat` race-proof without an in-memory lock anywhere in the process.

`acquire` folds "first claim" and "takeover of a dead/terminal claim"
into one function rather than exposing takeover separately — they're the
same code path in the same transaction, and splitting them would just
invite a caller to run them non-atomically. The contended branch locks
the row with `SELECT ... FOR UPDATE` before deciding
(.agents/skills/designing-application-transactions/SKILL.md §6): under
the two-region race, a plain re-`SELECT` here just means every contender
reads the same stale row and all but one lose the write anyway — locking
early turns that into a wait instead of a pile of wasted 40001 retries.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn

LEASE_SECONDS = 45
HEARTBEAT_SECONDS = 10

_TERMINAL_STATES = ("SUCCEEDED", "FAILED", "ABANDONED")


@dataclass(frozen=True)
class Claim:
    """Result of `acquire`. Exactly one of the following is true:

    - `won` — caller now owns the key at `fence`; launch the stage.
    - `reuse_artifact_id is not None` — a prior run already succeeded; reuse it.
    - otherwise — another live owner holds it; go `subscribe`.
    """

    won: bool
    fence: int | None = None
    took_over_from: str | None = None
    reuse_artifact_id: str | None = None
    owner: str | None = None
    owner_host: str | None = None
    owner_region: str | None = None
    owner_fence: int | None = None
    run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    """The row `complete` writes to `artifacts`. Content hashing (`artifact_id`)
    and S3 upload are the caller's job — fingerprint/canon.py and storage/s3.py.
    """

    artifact_id: str
    stage: str
    work_key: str
    s3_uri: str
    size_bytes: int
    env_fingerprint: str
    produced_by_run: uuid.UUID
    duration_ms: int
    vcpu: float
    mem_mib: int
    region: str


@dataclass(frozen=True)
class SubscribeResult:
    terminal_state: str  # SUCCEEDED | FAILED | ABANDONED
    artifact_id: str | None
    owner: str
    owner_host: str
    owner_region: str
    waited_s: float


def _now(cur: psycopg.Cursor) -> datetime:
    """DB-side clock, not the client's — comparing a local timestamp to a
    row's lease_expires_at would let clock skew decide who owns a claim."""
    cur.execute("SELECT now()")
    row = cur.fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def acquire(
    pool: ConnectionPool,
    work_key: str,
    stage: str,
    owner: str,
    host: str,
    region: str,
    run_id: uuid.UUID,
) -> Claim:
    def _tx(cur: psycopg.Cursor) -> Claim:
        cur.execute(
            """
            INSERT INTO work_claims
              (work_key, stage, state, owner_id, owner_host, owner_region,
               fence, lease_expires_at, run_id)
            VALUES (%s,%s,'CLAIMED',%s,%s,%s,1, now() + %s, %s)
            ON CONFLICT (work_key) DO NOTHING
            RETURNING fence
            """,
            (work_key, stage, owner, host, region, f"{LEASE_SECONDS} seconds", run_id),
        )
        row = cur.fetchone()
        if row:
            return Claim(won=True, fence=row[0])

        # Contended: lock the row before deciding (see module docstring).
        cur.execute(
            """
            SELECT state, owner_id, owner_host, owner_region, fence,
                   lease_expires_at, artifact_id, run_id
            FROM work_claims WHERE work_key=%s FOR UPDATE
            """,
            (work_key,),
        )
        found = cur.fetchone()
        assert found is not None, "row must exist: prior INSERT hit ON CONFLICT"
        st, o, oh, orr, fence, exp, art, rid = found

        if st == "SUCCEEDED":
            return Claim(won=False, reuse_artifact_id=art)

        is_terminal = st in ("FAILED", "ABANDONED")
        now_ts = _now(cur)
        if is_terminal or exp < now_ts:
            cur.execute(
                """
                UPDATE work_claims
                   SET state='CLAIMED', owner_id=%s, owner_host=%s, owner_region=%s,
                       fence=fence+1, lease_expires_at=now() + %s,
                       run_id=%s, updated_at=now()
                 WHERE work_key=%s
             RETURNING fence
                """,
                (owner, host, region, f"{LEASE_SECONDS} seconds", run_id, work_key),
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
                    work_key,
                    o,
                    owner,
                    fence,
                    new_fence,
                    "prior_terminal" if is_terminal else "lease_expired",
                ),
            )
            return Claim(won=True, fence=new_fence, took_over_from=o)

        return Claim(
            won=False,
            owner=o,
            owner_host=oh,
            owner_region=orr,
            owner_fence=fence,
            run_id=rid,
        )

    return in_txn(pool, _tx, op="claim.acquire")


def heartbeat(pool: ConnectionPool, work_key: str, owner: str, fence: int) -> bool:
    """Refresh the lease. Returns False iff the caller has been dispossessed
    (fence moved on, or the row is no longer CLAIMED/RUNNING) — that means
    stop immediately, do not write anything else under this fence."""

    def _tx(cur: psycopg.Cursor) -> bool:
        cur.execute(
            """UPDATE work_claims
                  SET lease_expires_at = now() + %s, updated_at = now()
                WHERE work_key=%s AND owner_id=%s AND fence=%s
                  AND state IN ('CLAIMED','RUNNING')""",
            (f"{LEASE_SECONDS} seconds", work_key, owner, fence),
        )
        return cur.rowcount == 1

    return in_txn(pool, _tx, op="claim.heartbeat")


def complete(
    pool: ConnectionPool, work_key: str, owner: str, fence: int, artifact: ArtifactRecord
) -> bool:
    """Insert the artifact and flip the claim to SUCCEEDED — one transaction,
    never two, so a crash between them is impossible by construction. The
    artifact insert is `ON CONFLICT DO NOTHING` on its content-addressed PK,
    so replaying this call (e.g. after a retry) is a genuine no-op."""

    def _tx(cur: psycopg.Cursor) -> bool:
        cur.execute(
            """
            INSERT INTO artifacts
              (artifact_id, stage, work_key, s3_uri, size_bytes, env_fingerprint,
               produced_by_run, duration_ms, vcpu, mem_mib, region)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (artifact_id) DO NOTHING
            """,
            (
                artifact.artifact_id,
                artifact.stage,
                artifact.work_key,
                artifact.s3_uri,
                artifact.size_bytes,
                artifact.env_fingerprint,
                artifact.produced_by_run,
                artifact.duration_ms,
                artifact.vcpu,
                artifact.mem_mib,
                artifact.region,
            ),
        )
        cur.execute(
            """
            UPDATE work_claims
               SET state='SUCCEEDED', artifact_id=%s, updated_at=now()
             WHERE work_key=%s AND owner_id=%s AND fence=%s
               AND state IN ('CLAIMED','RUNNING')
            """,
            (artifact.artifact_id, work_key, owner, fence),
        )
        return cur.rowcount == 1

    return in_txn(pool, _tx, op="claim.complete")


def fail(pool: ConnectionPool, work_key: str, owner: str, fence: int) -> bool:
    """Flip the claim to FAILED so the next contender can take over
    immediately instead of waiting out the lease. Structured failure detail
    (traceback, error class, remediation) is db/memory.py's job, not this
    table's — work_claims tracks ownership, not why a run died."""

    def _tx(cur: psycopg.Cursor) -> bool:
        cur.execute(
            """UPDATE work_claims SET state='FAILED', updated_at=now()
                WHERE work_key=%s AND owner_id=%s AND fence=%s
                  AND state IN ('CLAIMED','RUNNING')""",
            (work_key, owner, fence),
        )
        return cur.rowcount == 1

    return in_txn(pool, _tx, op="claim.fail")


def subscribe(
    pool: ConnectionPool,
    work_key: str,
    *,
    poll_interval_s: float = 1.0,
    max_wait_s: float = 600.0,
) -> SubscribeResult:
    """The loser's path: poll until the owner reaches a terminal state, then
    adopt its result. From the caller's point of view it computed the value —
    see PROJECT.md §4.2 "Loser behaviour"."""

    def _poll(cur: psycopg.Cursor) -> tuple[str, str, str, str, str | None]:
        cur.execute(
            """SELECT state, owner_id, owner_host, owner_region, artifact_id
                 FROM work_claims WHERE work_key=%s""",
            (work_key,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no claim exists for work_key={work_key!r}")
        return row

    started = time.monotonic()
    while True:
        state, owner_id, owner_host, owner_region, artifact_id = in_txn(
            pool, _poll, op="claim.subscribe_poll"
        )
        if state in _TERMINAL_STATES:
            return SubscribeResult(
                terminal_state=state,
                artifact_id=artifact_id,
                owner=owner_id,
                owner_host=owner_host,
                owner_region=owner_region,
                waited_s=time.monotonic() - started,
            )
        elapsed = time.monotonic() - started
        if elapsed > max_wait_s:
            raise TimeoutError(
                f"subscribe on work_key={work_key!r} timed out after {elapsed:.1f}s "
                f"waiting on owner={owner_id!r}"
            )
        time.sleep(poll_interval_s)
