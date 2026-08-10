"""50 concurrent callers on one generation -- Appendix E Day 5 gate.

Must yield exactly one owner, 50 idempotent interests (durable
work_subscribers rows), and at most one committed derivation. Real threads
against the real cluster, not a simulated/serialized stand-in -- the
adversarial contention `db/txn.py::in_txn`'s SERIALIZABLE retry is meant
to survive.

Uses a dedicated connection pool sized for 50 simultaneous callers rather
than `db.pool.get_pool()`'s shared max_size=10: db/pool.py's HikariCP-
derived sizing is deliberate for one process's normal workload, and 50
*concurrent* callers is really the case of 50 separate machines each with
their own small pool -- this test's own pool stands in for that, not a
reason to resize the shared one.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from psycopg_pool import ConnectionPool

from cairn.db import claims
from cairn.db import flight as flight_db
from cairn.db.claims import HEARTBEAT_SECONDS
from cairn.storage.cas import publish_blob

pytestmark = pytest.mark.integration

CALLERS = 50


@pytest.fixture
def race_pool(pool: ConnectionPool) -> Iterator[ConnectionPool]:  # noqa: ARG001 - depend on skip
    database_url = os.environ["CAIRN_DATABASE_URL"]
    # A shared/free-tier cluster caps total backend connections well below
    # one-per-caller; 50 threads still exercise genuine SERIALIZABLE
    # contention through a pool smaller than 50 -- the race is decided at
    # the transaction layer, not by literally holding 50 sockets open at
    # once, so the rest queue for a checkout rather than needing one each.
    dedicated = ConnectionPool(
        conninfo=database_url,
        min_size=2,
        max_size=15,
        timeout=90,
        kwargs={"application_name": "cairn-test-race50"},
        open=True,
    )
    yield dedicated
    dedicated.close()


def _attempt(
    pool: ConnectionPool, ns: str, sem_key: str, i: int, stop_heartbeat: threading.Event
) -> flight_db.GenerationClaim:
    claim = flight_db.open_generation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem_key,
        stage_label="race50",
        owner_id=f"worker-{i}-{uuid.uuid4().hex[:6]}",
        host="race-host",
        region="local",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    if claim.role is flight_db.ClaimRole.OWNER:
        # A real winner heartbeats continuously (HEARTBEAT_SECONDS=10) for
        # as long as it holds work, keeping its lease (45s) alive. Fifty
        # threads queued behind a small pool under heavy SERIALIZABLE
        # contention can legitimately take *minutes* to all resolve on a
        # shared cluster -- without continuous heartbeating here, a
        # straggler among them (not a simultaneous contender) would find a
        # genuinely stale lease and correctly take over: that's the
        # death/takeover feature working as designed, not the "50
        # simultaneous callers, one owner" property this test checks.
        #
        # This must run in its own thread, not block here: this function's
        # return value is a ThreadPoolExecutor future that the test waits
        # on for *every* one of the 50 callers before it ever sets
        # stop_heartbeat -- looping in this call would deadlock the owner
        # against its own future.
        assert claim.owner_id is not None and claim.fence is not None
        owner_id, claim_key, fence = claim.owner_id, claim.claim_key, claim.fence

        def _heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                claims.heartbeat(pool, claim_key, owner_id, fence)
                stop_heartbeat.wait(HEARTBEAT_SECONDS)

        threading.Thread(target=_heartbeat_loop, daemon=True).start()
    return claim


def _run_race(pool: ConnectionPool, ns: str, sem_key: str) -> list[flight_db.GenerationClaim]:
    stop_heartbeat = threading.Event()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CALLERS) as executor:
            futures = [
                executor.submit(_attempt, pool, ns, sem_key, i, stop_heartbeat)
                for i in range(CALLERS)
            ]
            results = [f.result() for f in futures]
    finally:
        stop_heartbeat.set()
    return results


def test_fifty_concurrent_callers_yield_one_owner_and_durable_interests(
    race_pool: ConnectionPool,
) -> None:
    ns = f"race50-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(race_pool, ns, ns)
    sem_key = hashlib.sha256(ns.encode()).hexdigest()

    results = _run_race(race_pool, ns, sem_key)

    owners = [r for r in results if r.role is flight_db.ClaimRole.OWNER]
    subscribers = [r for r in results if r.role is flight_db.ClaimRole.SUBSCRIBER]
    assert len(owners) == 1, f"expected exactly one owner, got {len(owners)}"
    assert len(subscribers) == CALLERS - 1

    with race_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM work_subscribers
                WHERE namespace_id=%s AND semantic_work_key=%s AND generation=1""",
            (ns, sem_key),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == CALLERS, "every caller (owner included) must get a durable interest row"


def test_at_most_one_committed_derivation_after_the_race(race_pool: ConnectionPool) -> None:
    bucket = os.environ.get("CAIRN_S3_BUCKET")
    if not bucket:
        pytest.skip("CAIRN_S3_BUCKET not set -- publish proof needs a real bucket")

    ns = f"race50-pub-{uuid.uuid4().hex[:8]}"
    flight_db.ensure_namespace(race_pool, ns, ns)
    sem_key = hashlib.sha256(ns.encode()).hexdigest()

    results = _run_race(race_pool, ns, sem_key)

    owner = next(r for r in results if r.role is flight_db.ClaimRole.OWNER)
    assert owner.fence is not None
    # publish_derivation re-checks {owner_id, run_id, fence, state} against the
    # live claim row; a fresh uuid here is a guaranteed REJECTED_FENCE even when
    # the race itself elected a single owner correctly.
    assert owner.owner_run_id is not None

    publication = publish_blob(bucket, f"race50-payload-{ns}".encode())
    outcome = flight_db.publish_derivation(
        race_pool,
        namespace_id=ns,
        semantic_work_key=sem_key,
        generation=1,
        derivation_id=uuid.uuid4(),
        publication_operation_id=uuid.uuid4(),
        blob=publication,
        observation_id=None,
        authority=flight_db.Authority.STRUCTURAL,
        run_id=owner.owner_run_id,
        owner_id=owner.owner_id or "",
        fence=owner.fence,
        stage_label="race50",
        decision_explanation="race50 winner publish",
    )
    assert outcome is flight_db.PublishOutcome.PUBLISHED

    with race_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM derivations WHERE namespace_id=%s AND semantic_work_key=%s",
            (ns, sem_key),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1, "at most one committed derivation for this generation"
