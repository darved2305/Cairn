#!/usr/bin/env python3
"""Measure post-first-microchunk timing window for Day 5 / Gate D.

Reads fragment_commits for one leaf under a parent root derivation + bucket.
Prints only measured timestamps — never invents a resumed count or margin.

docs/project/PLAN.md Day 4: if after the first 8-record microchunk there is not enough
useful work left for StopTask before leaf publication, Gate D falls back
to whole-stage takeover.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from cairn.db.pool import close_pool, get_pool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-derivation", required=True, help="Root derivation UUID")
    parser.add_argument("--bucket", type=int, required=True)
    args = parser.parse_args()
    try:
        parent = uuid.UUID(args.parent_derivation)
    except ValueError:
        print(json.dumps({"ok": False, "error": "parent derivation must be a UUID"}))
        return 2

    pool = get_pool()
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT cd.derivation_id, cd.namespace_id, cd.semantic_work_key,
                       cd.generation, cd.produced_by_run, cd.created_at
                  FROM derivation_fragments f
                  JOIN derivations cd ON cd.derivation_id = f.child_derivation_id
                 WHERE f.parent_derivation_id = %s AND f.ordinal = %s
                """,
                (parent, args.bucket),
            )
            leaf = cur.fetchone()
            if leaf is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "no leaf fragment for parent/bucket",
                            "parent": str(parent),
                            "bucket": args.bucket,
                        },
                        sort_keys=True,
                    )
                )
                return 1
            leaf_id, ns, sem, gen, produced_by, leaf_created = leaf
            cur.execute(
                """
                SELECT microchunk_key, created_at, committed_fence
                  FROM fragment_commits
                 WHERE namespace_id = %s AND semantic_work_key = %s AND generation = %s
                 ORDER BY created_at ASC
                """,
                (ns, sem, gen),
            )
            chunks = cur.fetchall()
    finally:
        close_pool()

    if not chunks:
        payload = {
            "ok": True,
            "parent_derivation_id": str(parent),
            "bucket": args.bucket,
            "leaf_derivation_id": str(leaf_id),
            "namespace_id": ns,
            "semantic_work_key": sem,
            "generation": gen,
            "microchunk_count": 0,
            "timing_margin_seconds": None,
            "day5_mode": "whole_stage_takeover",
            "reason": "no fragment_commits rows — cannot claim sub-leaf resume",
        }
        print(json.dumps(payload, sort_keys=True, default=str))
        return 0

    first_at = chunks[0][1]
    last_at = chunks[-1][1]
    leaf_done = leaf_created
    # Margin after first commit until leaf derivation row appears (publication).
    post_first_to_leaf = (leaf_done - first_at).total_seconds() if leaf_done and first_at else None
    inter_chunk = (last_at - first_at).total_seconds() if len(chunks) > 1 else 0.0
    # Sub-leaf resume needs >1 microchunk AND a real positive window after the first.
    mode = (
        "sub_leaf_resume_candidate"
        if len(chunks) > 1 and post_first_to_leaf is not None and post_first_to_leaf > 0
        else "whole_stage_takeover"
    )
    payload = {
        "ok": True,
        "parent_derivation_id": str(parent),
        "bucket": args.bucket,
        "leaf_derivation_id": str(leaf_id),
        "namespace_id": ns,
        "semantic_work_key": sem,
        "generation": gen,
        "microchunk_count": len(chunks),
        "first_microchunk_at": first_at.isoformat(),
        "last_microchunk_at": last_at.isoformat(),
        "leaf_created_at": leaf_done.isoformat() if leaf_done else None,
        "seconds_first_to_last_microchunk": inter_chunk,
        "seconds_first_microchunk_to_leaf_row": post_first_to_leaf,
        "day5_mode": mode,
        "reason": (
            "measured positive multi-microchunk window"
            if mode == "sub_leaf_resume_candidate"
            else "fewer than 2 microchunks or non-positive post-first window"
        ),
    }
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
