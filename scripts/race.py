#!/usr/bin/env python3
"""Two-worker duplicate-claim race driver — `make race`.

Runs N independent races, each on a fresh work_key, with two workers
hitting cairn.db.claims.acquire() concurrently against the real cluster.
Asserts exactly one winner per race and prints a pass/fail summary.

This is the local, single-machine version of the correctness proof;
D7 repeats the same protocol across two real ECS tasks in two AWS regions
instead of two local threads on one box.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from cairn.db import claims
from cairn.db.pool import close_pool, get_pool

STAGE = "race_driver"
REGION = "us-east-1"
WORKERS_PER_RACE = 2


def _race_once(pool, race_id: int) -> int:
    """Returns the number of winners (should always be 1)."""
    work_key = f"race-driver-{race_id}-{uuid.uuid4().hex[:8]}"
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
        results = [f.result() for f in as_completed(futures)]

    return sum(1 for r in results if r.won)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()

    pool = get_pool()
    try:
        bad_races: list[tuple[int, int]] = []
        for i in range(args.iterations):
            winners = _race_once(pool, i)
            if winners != 1:
                bad_races.append((i, winners))
                print(f"  race {i}: {winners} winners (want 1)", file=sys.stderr)
    finally:
        close_pool()

    passed = args.iterations - len(bad_races)
    print(f"{passed}/{args.iterations} races had exactly one winner")
    return 0 if not bad_races else 1


if __name__ == "__main__":
    sys.exit(main())
