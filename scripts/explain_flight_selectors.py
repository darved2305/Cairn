#!/usr/bin/env python3
"""EXPLAIN the Day-3 Flight Recorder selector queries against the live cluster.

Prints plans for validated-observation, current-derivation, reverse-invalidation,
and subscriber queries (§20). Does not invent indexes — reports what EXPLAIN says.
"""

from __future__ import annotations

import os
import sys

import psycopg

QUERIES: dict[str, str] = {
    "validated_observations": """
EXPLAIN
SELECT o.observation_id, o.trace_digest, o.semantic_work_key,
       t.input_resource_set_digest, t.coverage_state
FROM execution_specs AS s
JOIN trace_observations AS o
  ON o.spec_id = s.spec_id AND o.namespace_id = s.namespace_id
JOIN trace_contents AS t ON t.trace_digest = o.trace_digest
WHERE s.namespace_id = $1
  AND s.compatibility_key = $2
  AND o.lifecycle_state = 'VALIDATED'
ORDER BY o.observed_at DESC, o.observation_id DESC
LIMIT 8
""",
    "current_derivations": """
EXPLAIN
SELECT h.semantic_work_key, h.current_generation,
       d.derivation_id, d.blob_digest,
       b.bucket, b.object_key, b.version_id, b.checksum_sha256
FROM work_heads AS h
JOIN work_generations AS g
  ON g.namespace_id = h.namespace_id
 AND g.semantic_work_key = h.semantic_work_key
 AND g.generation = h.current_generation
JOIN derivations AS d ON d.derivation_id = g.current_derivation_id
JOIN content_blobs AS b ON b.blob_digest = d.blob_digest
JOIN trace_observations AS o
  ON o.observation_id = d.observation_id AND o.namespace_id = d.namespace_id
LEFT JOIN reuse_rule_heads AS rh ON rh.rule_id = d.rule_id
LEFT JOIN reuse_rule_revisions AS rr
  ON rr.rule_id = d.rule_id AND rr.revision = d.rule_revision
WHERE h.namespace_id = $1
  AND h.semantic_work_key = ANY($2)
  AND g.lifecycle_state = 'PUBLISHED'
  AND d.state = 'PUBLISHED'
  AND d.quarantined_at IS NULL
  AND b.integrity_state = 'VALID'
  AND o.lifecycle_state = 'VALIDATED'
  AND (
    d.rule_id IS NULL
    OR (
      rh.current_revision = d.rule_revision
      AND rr.state IN ('ACTIVE', 'TIGHTENED')
    )
  )
""",
    "reverse_invalidation_by_blob": """
EXPLAIN
SELECT d.namespace_id, d.semantic_work_key, d.generation, d.derivation_id
FROM derivations AS d
WHERE d.blob_digest = $1
  AND d.state = 'PUBLISHED'
""",
    "live_subscribers": """
EXPLAIN
SELECT subscriber_id, request_id, run_id, lease_expires_at
FROM work_subscribers
WHERE namespace_id = $1 AND semantic_work_key = $2 AND generation = $3
  AND state = 'LIVE' AND lease_expires_at > now()
""",
}


def main() -> int:
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        print("CAIRN_DATABASE_URL is not set", file=sys.stderr)
        return 2
    dummy_digest = "0" * 64
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for name, sql in QUERIES.items():
            print(f"\n=== {name} ===")
            if name == "validated_observations":
                cur.execute(
                    sql.replace("$1", "%s").replace("$2", "%s"),
                    ("local", dummy_digest),
                )
            elif name == "current_derivations":
                cur.execute(
                    sql.replace("$1", "%s").replace("$2", "%s"),
                    ("local", [dummy_digest]),
                )
            elif name == "reverse_invalidation_by_blob":
                cur.execute(sql.replace("$1", "%s"), (dummy_digest,))
            else:
                cur.execute(
                    sql.replace("$1", "%s").replace("$2", "%s").replace("$3", "%s"),
                    ("local", dummy_digest, 1),
                )
            for row in cur.fetchall():
                text = row[0] if len(row) == 1 else str(row)
                sys.stdout.buffer.write((str(text) + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
