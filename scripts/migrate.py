#!/usr/bin/env python3
"""Apply db/migrations/* in order, tracked in schema_migrations.

Each DDL statement runs individually and idempotently. The migration marker
is written only after every postcondition for that file holds — so killing
the runner between statements cannot leave a false "applied" row.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
from psycopg.errors import DuplicateObject, DuplicateTable

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_OPTIONAL_VECTOR_INDEX = re.compile(r"^\s*CREATE\s+VECTOR\s+INDEX\b", re.IGNORECASE)
_CLUSTER_SETTING = re.compile(r"^\s*SET\s+CLUSTER\s+SETTING\b", re.IGNORECASE)


class MigrationKill(SystemExit):
    """Raised by tests to simulate a kill after N statements."""


def _database_url() -> str:
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        raise SystemExit(
            "CAIRN_DATABASE_URL is not set. Run scripts/provision_cluster.sh "
            "first, or export it yourself for a local cluster."
        )
    return url


def split_sql_statements(sql: str) -> list[str]:
    """Split a migration file into executable statements.

    Strips ``--`` line comments and respects single-quoted string literals
    so a semicolon inside a string does not split wrongly.
    """
    kept: list[str] = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        kept.append(line)
    text = "\n".join(kept)
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_single:
            in_single = True
            buf.append(ch)
        elif ch == "'" and in_single:
            if i + 1 < len(text) and text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single = False
            buf.append(ch)
        elif ch == ";" and not in_single:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    trailing = "".join(buf).strip()
    if trailing:
        statements.append(trailing)
    return statements


def _migration_files() -> list[Path]:
    files = [
        p
        for p in MIGRATIONS_DIR.iterdir()
        if p.is_file() and p.suffix in {".sql", ".py"} and not p.name.startswith("_")
    ]
    return sorted(files, key=lambda p: p.name)


def _pending(cur: psycopg.Cursor[Any], files: list[Path]) -> list[Path]:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version STRING PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    cur.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}
    return [f for f in files if f.name not in applied]


def _postconditions_0010(cur: psycopg.Cursor[Any]) -> None:
    required_tables = (
        "namespaces",
        "execution_specs",
        "trace_contents",
        "trace_resources",
        "trace_observations",
        "content_blobs",
        "work_heads",
        "work_generations",
        "derivations",
        "fragment_commits",
        "failure_embedding_revisions",
        "work_subscribers",
        "reuse_rule_revisions",
        "reuse_rule_heads",
    )
    for table in required_tables:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        if cur.fetchone() is None:
            raise RuntimeError(f"postcondition failed: table {table} missing after 0010")
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'work_claims' AND column_name = 'derivation_id'"
    )
    if cur.fetchone() is None:
        raise RuntimeError("postcondition failed: work_claims.derivation_id missing")


def _postconditions_0011(cur: psycopg.Cursor[Any]) -> None:
    cur.execute("SHOW CONSTRAINTS FROM work_claims")
    rows = cur.fetchall()
    texts = [" ".join(str(c) for c in row).lower() for row in rows]
    for text in texts:
        if "work_claims_success_pointer" in text:
            continue
        if (
            "succeeded" in text
            and "artifact_id" in text
            and "derivation_id" not in text
            and "state" in text
        ):
            raise RuntimeError(
                "postcondition failed: unnamed SUCCEEDED=>artifact_id CHECK still present"
            )
    if not any("work_claims_success_pointer" in text for text in texts):
        raise RuntimeError("postcondition failed: work_claims_success_pointer missing")


_POSTCONDITIONS: dict[str, Callable[[psycopg.Cursor[Any]], None]] = {
    "0010_flight_recorder.sql": _postconditions_0010,
    "0011_claim_pointer_constraint.py": _postconditions_0011,
}


def _execute_sql_statement(cur: psycopg.Cursor[Any], stmt: str) -> None:
    try:
        cur.execute(stmt)
    except (DuplicateObject, DuplicateTable):
        # Resume after a mid-file kill: CREATE TABLE / INDEX / CONSTRAINT
        # without IF NOT EXISTS must still converge (Appendix E Day-1 gate).
        return
    except Exception as exc:
        if _OPTIONAL_VECTOR_INDEX.match(stmt) or _CLUSTER_SETTING.match(stmt):
            print(f"  optional skipped: {exc}")
            return
        raise


def apply_sql_migration(
    conn: psycopg.Connection[Any],
    path: Path,
    *,
    before_statement: Callable[[int, str], None] | None = None,
    global_index_start: int = 0,
) -> int:
    statements = split_sql_statements(path.read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        for offset, stmt in enumerate(statements):
            if before_statement is not None:
                before_statement(global_index_start + offset, stmt)
            _execute_sql_statement(cur, stmt)
        post = _POSTCONDITIONS.get(path.name)
        if post is not None:
            post(cur)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (path.name,),
        )
    return len(statements)


def apply_python_migration(
    conn: psycopg.Connection[Any],
    path: Path,
    *,
    before_statement: Callable[[int, str], None] | None = None,
    global_index_start: int = 0,
) -> int:
    spec = importlib.util.spec_from_file_location(f"cairn_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load migration module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    apply_fn = getattr(module, "apply", None)
    if not callable(apply_fn):
        raise RuntimeError(f"migration {path.name} must define apply(conn)")

    steps = {"n": 0}

    def _hook(i: int, label: str) -> None:
        steps["n"] = i + 1
        if before_statement is not None:
            before_statement(global_index_start + i, label)

    apply_fn(conn, on_statement=_hook)
    with conn.cursor() as cur:
        post = _POSTCONDITIONS.get(path.name)
        if post is not None:
            post(cur)
        cur.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (path.name,),
        )
    return max(steps["n"], 1)


def apply_all(
    database_url: str | None = None,
    *,
    kill_after_statement: int | None = None,
) -> None:
    """Apply pending migrations.

    ``kill_after_statement=N`` executes N statements then raises MigrationKill
    without writing the current file's marker (0-based: N=1 runs one statement).
    """
    url = database_url or _database_url()
    files = _migration_files()
    global_index = 0

    def before_statement(index: int, stmt: str) -> None:
        if kill_after_statement is not None and index >= kill_after_statement:
            preview = stmt.replace("\n", " ")[:80]
            raise MigrationKill(f"killed before statement index {index}: {preview}")

    with psycopg.connect(url, autocommit=True) as conn:
        with conn.cursor() as cur:
            pending = _pending(cur, files)
        if not pending:
            print("schema up to date, nothing to apply")
            return
        for path in pending:
            print(f"applying {path.name} ...")
            try:
                if path.suffix == ".sql":
                    n = apply_sql_migration(
                        conn,
                        path,
                        before_statement=before_statement,
                        global_index_start=global_index,
                    )
                else:
                    n = apply_python_migration(
                        conn,
                        path,
                        before_statement=before_statement,
                        global_index_start=global_index,
                    )
                global_index += n
                print(f"  applied {path.name}")
            except MigrationKill:
                print(f"  killed while applying {path.name} (marker not written)")
                raise


def main() -> int:
    try:
        apply_all()
    except MigrationKill as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
