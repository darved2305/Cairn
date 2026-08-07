"""Shared `environments` upsert — every artifact's `env_fingerprint` column
is a foreign key into this table, so anything about to write an artifact
must ensure the row exists first. One real implementation shared by
`cli.py` (claim-demo, plan --persist-graph) and `agent/loop.py`, rather
than two copies drifting apart."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn


def ensure_environment(
    pool: ConnectionPool,
    *,
    env_fingerprint: str,
    image_digest: str,
    python_version: str,
    deps: dict[str, str],
    torch_threads: int,
) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO environments (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (env_fingerprint) DO NOTHING
            """,
            (env_fingerprint, image_digest, python_version, Jsonb(deps), torch_threads),
        )

    in_txn(pool, _tx, op="environments.ensure_environment")
