"""0011 — drop the unnamed SUCCEEDED=>artifact_id CHECK from 0001.

0001 created ``CHECK (state <> 'SUCCEEDED' OR artifact_id IS NOT NULL)``
without an explicit name. CockroachDB assigns a generated name
(``check_state_artifact_id`` on this engine); guessing it is how migrations
break across clusters. This migration uses SHOW CONSTRAINTS, matches the
normalized expression exactly, quotes/drops that discovered name, asserts
it is gone, and relies on 0010's ``work_claims_success_pointer`` as the
stable replacement (artifact XOR derivation).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import psycopg

_LEGACY_EXPR = re.compile(
    r"\(\(state\s*!=\s*'SUCCEEDED'::STRING\)\s*OR\s*\(artifact_id\s+IS\s+NOT\s+NULL\)\)"
    r"|state\s*(?:<>|!=)\s*'SUCCEEDED'\s*OR\s*artifact_id\s+IS\s+NOT\s+NULL",
    re.IGNORECASE,
)


def _find_legacy_constraint_name(cur: psycopg.Cursor[Any]) -> str | None:
    cur.execute("SHOW CONSTRAINTS FROM work_claims")
    columns = [desc.name.lower() for desc in (cur.description or [])]
    rows = cur.fetchall()
    try:
        name_idx = columns.index("constraint_name")
    except ValueError:
        name_idx = next((i for i, c in enumerate(columns) if c.endswith("constraint_name")), 1)
    try:
        details_idx = columns.index("details")
    except ValueError:
        details_idx = next(
            (i for i, c in enumerate(columns) if c in {"details", "check", "definition"}),
            -1,
        )
    try:
        type_idx = columns.index("constraint_type")
    except ValueError:
        type_idx = -1

    for row in rows:
        name = str(row[name_idx])
        if name in {
            "work_claims_success_pointer",
            "work_claims_state_membership",
            "work_claims_pkey",
            "claim_derivation_fk",
        }:
            continue
        ctype = str(row[type_idx]).upper() if type_idx >= 0 else ""
        if ctype and ctype != "CHECK":
            continue
        expr = str(row[details_idx]) if details_idx >= 0 else " ".join(str(c) for c in row)
        # Legacy check mentions artifact_id and SUCCEEDED but NOT derivation_id.
        if "derivation_id" in expr.lower():
            continue
        if _LEGACY_EXPR.search(expr) or (
            "succeeded" in expr.lower()
            and "artifact_id" in expr.lower()
            and "derivation_id" not in expr.lower()
        ):
            return name
    return None


def apply(
    conn: psycopg.Connection[Any],
    *,
    on_statement: Callable[[int, str], None] | None = None,
) -> None:
    step = 0

    def _emit(label: str) -> None:
        nonlocal step
        if on_statement is not None:
            on_statement(step, label)
        step += 1

    with conn.cursor() as cur:
        _emit("SHOW CONSTRAINTS FROM work_claims")
        name = _find_legacy_constraint_name(cur)
        if name is None:
            cur.execute("SHOW CONSTRAINTS FROM work_claims")
            texts = [" ".join(str(c) for c in row).lower() for row in cur.fetchall()]
            if not any("work_claims_success_pointer" in t for t in texts):
                raise RuntimeError(
                    "legacy SUCCEEDED=>artifact_id CHECK not found and "
                    "work_claims_success_pointer is also missing"
                )
            return

        drop_sql = f'ALTER TABLE work_claims DROP CONSTRAINT IF EXISTS "{name}"'
        _emit(drop_sql)
        cur.execute(drop_sql)

        _emit("assert legacy CHECK gone")
        remaining = _find_legacy_constraint_name(cur)
        if remaining is not None:
            raise RuntimeError(f"failed to drop legacy constraint {remaining!r}")
