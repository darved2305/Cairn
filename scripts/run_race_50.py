#!/usr/bin/env python3
"""Run the Appendix E Day-5 race gate against a live CockroachDB cluster.

Mirrors ``tests/integration/test_race_50.py`` without requiring pytest in
the worker image (``Dockerfile`` installs ``--no-dev``). Fifty concurrent
callers on one generation must yield one owner, CALLERS durable interests,
and at most one committed derivation after the winner publishes.

Uses a dedicated pool (max_size=15), not ``db/pool.py``'s shared max_size=10.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from psycopg_pool import ConnectionPool

from cairn.db import claims
from cairn.db import flight as flight_db
from cairn.db.claims import HEARTBEAT_SECONDS
from cairn.storage.cas import publish_blob

CALLERS = 50


def _attempt(
    pool: ConnectionPool,
    ns: str,
    sem_key: str,
    i: int,
    stop_heartbeat: threading.Event,
) -> flight_db.GenerationClaim:
    claim = flight_db.open_generation(
        pool,
        namespace_id=ns,
        semantic_work_key=sem_key,
        stage_label="race50",
        owner_id=f"worker-{i}-{uuid.uuid4().hex[:6]}",
        host="race-host",
        region="ecs",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
    )
    if claim.role is flight_db.ClaimRole.OWNER:
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


def main() -> int:
    database_url = os.environ["CAIRN_DATABASE_URL"]
    bucket = os.environ.get("CAIRN_S3_BUCKET")
    out_dir = Path(os.environ.get("CAIRN_RACE50_OUT", "/tmp/race50"))
    out_dir.mkdir(parents=True, exist_ok=True)

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=2,
        max_size=15,
        timeout=90,
        kwargs={"application_name": "cairn-test-race50"},
        open=True,
    )
    t0 = time.monotonic()
    result: dict[str, object] = {"ok": False, "callers": CALLERS}
    try:
        ns = f"race50-{uuid.uuid4().hex[:8]}"
        flight_db.ensure_namespace(pool, ns, ns)
        sem_key = hashlib.sha256(ns.encode()).hexdigest()
        result["namespace"] = ns
        result["semantic_work_key"] = sem_key

        results = _run_race(pool, ns, sem_key)
        owners = [r for r in results if r.role is flight_db.ClaimRole.OWNER]
        subscribers = [r for r in results if r.role is flight_db.ClaimRole.SUBSCRIBER]
        result["owners"] = len(owners)
        result["subscribers"] = len(subscribers)
        if len(owners) != 1:
            result["error"] = f"expected exactly one owner, got {len(owners)}"
            return 2
        if len(subscribers) != CALLERS - 1:
            result["error"] = f"expected {CALLERS - 1} subscribers, got {len(subscribers)}"
            return 2

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM work_subscribers
                    WHERE namespace_id=%s AND semantic_work_key=%s AND generation=1""",
                (ns, sem_key),
            )
            row = cur.fetchone()
            interests = int(row[0]) if row is not None else -1
        result["interests"] = interests
        if interests != CALLERS:
            result["error"] = f"expected {CALLERS} durable interests, got {interests}"
            return 2

        if not bucket:
            result["publish_skipped"] = "CAIRN_S3_BUCKET not set"
            result["ok"] = True
            return 0

        owner = owners[0]
        assert owner.fence is not None
        # Must be the claim's live run_id — a fresh uuid is REJECTED_FENCE.
        assert owner.owner_run_id is not None
        publication = publish_blob(bucket, f"race50-payload-{ns}".encode())
        outcome = flight_db.publish_derivation(
            pool,
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
        result["publish_outcome"] = outcome.name
        if outcome is not flight_db.PublishOutcome.PUBLISHED:
            result["error"] = f"publish failed: {outcome}"
            return 3

        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM derivations WHERE namespace_id=%s AND semantic_work_key=%s",
                (ns, sem_key),
            )
            row = cur.fetchone()
            derivations = int(row[0]) if row is not None else -1
        result["derivations"] = derivations
        if derivations != 1:
            result["error"] = f"expected 1 derivation, got {derivations}"
            return 4

        result["ok"] = True
        return 0
    except Exception as exc:  # noqa: BLE001 - gate script surfaces the exact failure
        result["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        result["elapsed_s"] = round(time.monotonic() - t0, 3)
        text = json.dumps(result, sort_keys=True, indent=2)
        (out_dir / "RESULT.json").write_text(text + "\n", encoding="utf-8")
        print(text, flush=True)
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
