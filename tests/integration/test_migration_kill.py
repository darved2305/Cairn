"""Migrations 0001→0011 must converge even when the runner is killed mid-file."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import migrate as migrate_mod  # noqa: E402

pytestmark = pytest.mark.integration


def _fresh_database_url() -> str:
    base = os.environ.get("CAIRN_DATABASE_URL")
    if not base:
        pytest.skip("CAIRN_DATABASE_URL not set")
    # Create an isolated database so kill/resume does not poison the shared cluster.
    db_name = f"cairn_mig_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{db_name}"')
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


def test_migrations_converge_after_kill_between_statements() -> None:
    url = _fresh_database_url()
    with pytest.raises(migrate_mod.MigrationKill):
        migrate_mod.apply_all(url, kill_after_statement=3)

    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = [row[0] for row in cur.fetchall()]
        assert "0001_init.sql" not in applied

    migrate_mod.apply_all(url)

    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        versions = [row[0] for row in cur.fetchall()]
        assert "0010_flight_recorder.sql" in versions
        assert "0011_claim_pointer_constraint.py" in versions
        assert len(versions) == len(set(versions))
        cur.execute("SHOW CONSTRAINTS FROM work_claims")
        texts = [" ".join(str(c) for c in row).lower() for row in cur.fetchall()]
        assert any("work_claims_success_pointer" in t for t in texts)
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'fragment_commits'"
        )
        assert cur.fetchone() is not None
