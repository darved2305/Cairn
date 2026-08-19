"""Integration tests for cairn.db.claims — real CockroachDB, no mocks.

Covers docs/project/PLAN.md D2's four required proofs:
  1. 2 workers, same key, 200 iterations -> exactly one winner, every time.
  2. the loser subscribes and receives the artifact id.
  3. a dispossessed worker's fenced write affects 0 rows.
  4. takeover is refused while the lease is live, permitted once it's
     expired, and fence is strictly monotone across takeovers.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg.types.json import Jsonb

from cairn.db import claims
from cairn.db.txn import in_txn

pytestmark = pytest.mark.integration

STAGE = "race_test"
REGION = "us-east-1"
RACE_ITERATIONS = 200
WORKERS_PER_RACE = 2


def _ensure_test_environment(pool, env_fingerprint: str) -> None:
    def _tx(cur):
        cur.execute(
            """INSERT INTO environments
                 (env_fingerprint, image_digest, python_version, deps, torch_threads)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (env_fingerprint) DO NOTHING""",
            (env_fingerprint, "sha256:test-fixture", "3.12.0", Jsonb({"fixture": True}), 1),
        )

    in_txn(pool, _tx, op="test.ensure_environment")


def _force_expire(pool, work_key: str) -> None:
    """Backdate a claim's lease so the next acquire() sees it as expired,
    without waiting out the real 45s LEASE_SECONDS in test time."""

    def _tx(cur):
        cur.execute(
            "UPDATE work_claims SET lease_expires_at = now() - interval '1 second' "
            "WHERE work_key=%s",
            (work_key,),
        )

    in_txn(pool, _tx, op="test.force_expire")


def _fresh_artifact(
    work_key: str, env_fingerprint: str, run_id: uuid.UUID
) -> claims.ArtifactRecord:
    return claims.ArtifactRecord(
        artifact_id=hashlib.sha256(f"{work_key}:{run_id}".encode()).hexdigest(),
        stage=STAGE,
        work_key=work_key,
        s3_uri=f"s3://cairn-test/{work_key}",
        size_bytes=128,
        env_fingerprint=env_fingerprint,
        produced_by_run=run_id,
        duration_ms=42,
        vcpu=2.0,
        mem_mib=4096,
        region=REGION,
    )


def _race_once(pool, race_id: int) -> list[claims.Claim]:
    work_key = f"race-{race_id}-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    def _attempt(worker_idx: int) -> claims.Claim:
        return claims.acquire(
            pool,
            work_key,
            STAGE,
            owner=f"worker-{worker_idx}-{uuid.uuid4().hex[:6]}",
            host="localhost",
            region=REGION,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=WORKERS_PER_RACE) as executor:
        futures = [executor.submit(_attempt, i) for i in range(WORKERS_PER_RACE)]
        return [f.result() for f in as_completed(futures)]


def test_race_produces_exactly_one_winner_every_time(pool) -> None:
    failures = []
    for i in range(RACE_ITERATIONS):
        results = _race_once(pool, i)
        winners = [r for r in results if r.won]
        if len(winners) != 1:
            failures.append((i, len(winners)))

    assert failures == [], (
        f"{len(failures)}/{RACE_ITERATIONS} races had != 1 winner: {failures[:10]}"
    )


def test_loser_subscribes_and_adopts_artifact(pool) -> None:
    work_key = f"subscribe-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    winner = claims.acquire(pool, work_key, STAGE, "owner-winner", "localhost", REGION, run_id)
    assert winner.won
    loser = claims.acquire(pool, work_key, STAGE, "owner-loser", "localhost", REGION, run_id)
    assert not loser.won
    assert loser.owner == "owner-winner"

    outcome: dict[str, claims.SubscribeResult] = {}

    def _subscribe() -> None:
        outcome["result"] = claims.subscribe(pool, work_key, poll_interval_s=0.2, max_wait_s=15.0)

    subscriber = threading.Thread(target=_subscribe)
    subscriber.start()

    env_fingerprint = f"test-env-{uuid.uuid4().hex[:8]}"
    _ensure_test_environment(pool, env_fingerprint)
    artifact = _fresh_artifact(work_key, env_fingerprint, run_id)
    time.sleep(0.5)  # let the subscriber's first poll land on CLAIMED, not SUCCEEDED
    assert claims.complete(pool, work_key, "owner-winner", winner.fence, artifact)

    subscriber.join(timeout=20)
    assert not subscriber.is_alive(), "subscribe() never returned"
    result = outcome["result"]
    assert result.terminal_state == "SUCCEEDED"
    assert result.artifact_id == artifact.artifact_id


def test_dispossessed_worker_fenced_write_affects_zero_rows(pool) -> None:
    work_key = f"dispossess-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    first = claims.acquire(pool, work_key, STAGE, "owner-a", "localhost", REGION, run_id)
    assert first.won

    _force_expire(pool, work_key)

    second = claims.acquire(pool, work_key, STAGE, "owner-b", "localhost", REGION, run_id)
    assert second.won
    assert second.took_over_from == "owner-a"
    assert second.fence == first.fence + 1

    # owner-a's world hasn't changed from its own point of view — it still
    # thinks it holds fence=first.fence. That write must affect zero rows.
    assert claims.heartbeat(pool, work_key, "owner-a", first.fence) is False


def test_live_claim_refuses_takeover(pool) -> None:
    work_key = f"no-takeover-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    first = claims.acquire(pool, work_key, STAGE, "owner-a", "localhost", REGION, run_id)
    assert first.won

    second = claims.acquire(pool, work_key, STAGE, "owner-b", "localhost", REGION, run_id)
    assert not second.won
    assert second.owner == "owner-a"
    assert second.owner_fence == first.fence


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cycles=st.integers(min_value=1, max_value=4))
def test_fence_strictly_increases_across_takeovers(pool, cycles: int) -> None:
    # 500 examples (docs/project/PLAN.md's default property-test budget) assumes a pure,
    # DB-free function; this property is asserted over real transactions
    # against a live cluster, so the example count is deliberately smaller.
    #
    # function_scoped_fixture is suppressed deliberately, not blindly: the
    # `pool` fixture (tests/integration/conftest.py) is function-scoped so
    # that a test which closes the process-wide pool (test_cli_claim_demo.py)
    # can't poison a session-cached pool object for every later test. That
    # doesn't make `pool` unstable *within* one test's Hypothesis example
    # loop — get_pool() still returns the same live ConnectionPool for the
    # whole duration of this test function, so nothing here can produce the
    # subtle cross-example leakage this health check exists to catch.
    work_key = f"fence-monotone-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    claim = claims.acquire(pool, work_key, STAGE, "owner-0", "localhost", REGION, run_id)
    assert claim.won
    fences = [claim.fence]

    for i in range(cycles):
        _force_expire(pool, work_key)
        claim = claims.acquire(pool, work_key, STAGE, f"owner-{i + 1}", "localhost", REGION, run_id)
        assert claim.won
        assert claim.took_over_from == f"owner-{i}"
        fences.append(claim.fence)

    assert fences == sorted(fences)
    assert len(set(fences)) == len(fences)
