#!/usr/bin/env python3
"""Apply db/migrations/*.sql, in order, tracked in schema_migrations.

No external `psql` dependency — CockroachDB speaks the Postgres wire
protocol, so psycopg alone is enough, and that keeps `make migrate`
working on any machine that already has the Python env set up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _database_url() -> str:
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        raise SystemExit(
            "CAIRN_DATABASE_URL is not set. Run scripts/provision_cluster.sh "
            "first, or export it yourself for a local cluster."
        )
    return url


def _pending(cur: psycopg.Cursor, files: list[Path]) -> list[Path]:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version STRING PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    cur.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}
    return [f for f in files if f.name not in applied]


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"no migrations found in {MIGRATIONS_DIR}")
        return 0

    with psycopg.connect(_database_url(), autocommit=True) as conn:
        with conn.cursor() as cur:
            pending = _pending(cur, files)

        if not pending:
            print("schema up to date, nothing to apply")
            return 0

        for path in pending:
            print(f"applying {path.name} ...")
            sql = path.read_text(encoding="utf-8")
            # Each migration runs as its own statement batch — DDL in
            # CockroachDB is transactional per statement, and splitting
            # avoids the "DDL statement mixed with other statements"
            # constraint that some multi-statement bodies hit.
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            print(f"  applied {path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
