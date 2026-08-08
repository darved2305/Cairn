"""The SERIALIZABLE retry wrapper. Every write in Cairn goes through this.

CockroachDB runs SERIALIZABLE by default and pushes conflict resolution
back to the client as a 40001 (`SerializationFailure`). The
designing-application-transactions skill is explicit that the retry unit
is the *whole* transaction, never a single statement
(.agents/skills/designing-application-transactions/SKILL.md §3) — a
partial replay can commit a mix of pre- and post-conflict state. Every
`fn` passed to `in_txn` must therefore be a pure function of its
arguments: no S3 puts, no Bedrock calls, no logging with side effects
beyond the cursor. A dropped or duplicated retry of a stray API call is
how you get a training run kicked off twice.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import psycopg
from psycopg.errors import SerializationFailure
from psycopg_pool import ConnectionPool, PoolTimeout

from cairn.obs.metrics import emit_metric

logger = logging.getLogger("cairn.db.txn")

MAX_ATTEMPTS = 8
BASE_MS = 50
CAP_MS = 2000

# Transient connectivity (timeout/reset/DNS blip against the real
# CockroachDB Cloud cluster) is a different failure class from 40001
# contention and gets its own small, separately-budgeted retry — the
# jittered exponential backoff above is tuned for contention, not network
# flakiness, and shouldn't govern both.
CONN_MAX_ATTEMPTS = 3
CONN_BASE_MS = 250


class TxnRetriesExhausted(RuntimeError):
    """Raised when a transaction closure still hits 40001 after MAX_ATTEMPTS."""


def in_txn[T](pool: ConnectionPool, fn: Callable[[psycopg.Cursor], T], *, op: str) -> T:
    """Run fn(cur) inside one SERIALIZABLE transaction, replaying the whole
    closure on SQLSTATE 40001.

    fn MUST be a pure function of its arguments — see module docstring.
    `op` is a short label (e.g. "claim.acquire") used only for logging;
    it has no effect on retry behaviour.
    """
    last: SerializationFailure | psycopg.OperationalError | None = None
    conn_attempts = 0
    for attempt in range(MAX_ATTEMPTS):
        try:
            with pool.connection() as conn:
                conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
            if attempt:
                logger.info(
                    "txn '%s' committed after %d retr%s",
                    op,
                    attempt,
                    "y" if attempt == 1 else "ies",
                    extra={"op": op, "retries": attempt, "metric": "TxnRetries40001"},
                )
                emit_metric("TxnRetries40001", attempt, dimensions={"op": op})
            return result
        except SerializationFailure as e:
            last = e
            backoff_ms = min(CAP_MS, BASE_MS * (2**attempt))
            jittered_s = (backoff_ms * random.uniform(0.75, 1.25)) / 1000.0
            logger.debug(
                "txn '%s' hit 40001 on attempt %d/%d, backing off %.0fms",
                op,
                attempt + 1,
                MAX_ATTEMPTS,
                jittered_s * 1000,
            )
            time.sleep(jittered_s)
        except PoolTimeout:
            # PoolTimeout already means the pool spent its own full
            # checkout timeout (db/pool.py) retrying internally — it IS
            # an exhausted wait, not a fast single-attempt failure, so
            # retrying it here would just stack another full wait on top
            # for no benefit. Let it propagate immediately.
            raise
        except psycopg.OperationalError as e:
            last = e
            conn_attempts += 1
            if conn_attempts > CONN_MAX_ATTEMPTS:
                raise
            backoff_s = (CONN_BASE_MS * conn_attempts) / 1000.0
            logger.warning(
                "txn '%s' hit a connection error on attempt %d/%d, retrying in %.0fms: %s",
                op,
                conn_attempts,
                CONN_MAX_ATTEMPTS,
                backoff_s * 1000,
                e,
            )
            time.sleep(backoff_s)
    raise TxnRetriesExhausted(f"txn '{op}' exhausted {MAX_ATTEMPTS} retries") from last
