"""lambda/reaper/handler.py — PROJECT.md §4.5, D7. Loaded by file path
rather than a normal import: `lambda` is a reserved Python keyword, so
`from lambda.reaper import handler` is a SyntaxError, not just an
unusual-but-legal import. The reaper is deliberately independent of the
`cairn` package (see handler.py's module docstring), so this test talks
to the database directly rather than through cairn.db.claims, matching
what the reaper itself does.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.integration

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda" / "reaper" / "handler.py"

# The real handler sweeps twice, REAPER_SWEEP_GAP_SECONDS apart (default
# 30s — see infra/events.tf's comment on why). Tests only care that both
# sweeps happen, not that they're 30 real seconds apart, so this is
# pinned tiny here.
os.environ.setdefault("REAPER_SWEEP_GAP_SECONDS", "0.2")


def _load_handler():
    spec = importlib.util.spec_from_file_location("reaper_handler", _HANDLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reaper = _load_handler()


def _insert_claim(conninfo: str, work_key: str, *, state: str, lease_offset_seconds: float) -> None:
    # SUCCEEDED rows require a non-null artifact_id (work_claims' own CHECK
    # constraint) — irrelevant to the reaper (SUCCEEDED is terminal, the
    # reaper should never touch it either way), but the insert has to
    # satisfy it regardless of which state this test is exercising.
    artifact_id = f"artifact-{uuid.uuid4().hex}" if state == "SUCCEEDED" else None
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO work_claims
              (work_key, stage, state, owner_id, owner_host, owner_region,
               fence, lease_expires_at, run_id, artifact_id)
            VALUES (%s, 'race_driver', %s, 'dead-owner', 'dead-host', 'us-east-1',
                    1, now() + %s * interval '1 second', gen_random_uuid(), %s)
            """,
            (work_key, state, lease_offset_seconds, artifact_id),
        )


def _fetch_state(conninfo: str, work_key: str) -> str:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        row = conn.execute(
            "SELECT state FROM work_claims WHERE work_key=%s", (work_key,)
        ).fetchone()
        assert row is not None
        return str(row[0])


def test_reaper_abandons_expired_claimed_leases(pool) -> None:
    conninfo = os.environ["CAIRN_DATABASE_URL"]
    work_key = f"reaper-expired-{uuid.uuid4().hex}"
    _insert_claim(conninfo, work_key, state="CLAIMED", lease_offset_seconds=-5)

    result = reaper.handler({}, None)

    assert result["reaped_count"] >= 1
    assert _fetch_state(conninfo, work_key) == "ABANDONED"


def test_reaper_does_not_touch_live_leases(pool) -> None:
    conninfo = os.environ["CAIRN_DATABASE_URL"]
    work_key = f"reaper-live-{uuid.uuid4().hex}"
    _insert_claim(conninfo, work_key, state="RUNNING", lease_offset_seconds=300)

    reaper.handler({}, None)

    assert _fetch_state(conninfo, work_key) == "RUNNING"


def test_reaper_does_not_touch_terminal_states(pool) -> None:
    conninfo = os.environ["CAIRN_DATABASE_URL"]
    work_key = f"reaper-terminal-{uuid.uuid4().hex}"
    _insert_claim(conninfo, work_key, state="SUCCEEDED", lease_offset_seconds=-5)

    reaper.handler({}, None)

    # SUCCEEDED with an expired lease is normal (leases aren't renewed
    # once a claim is terminal) — the reaper must never touch it.
    assert _fetch_state(conninfo, work_key) == "SUCCEEDED"


def test_reaper_is_idempotent_on_a_second_sweep(pool) -> None:
    conninfo = os.environ["CAIRN_DATABASE_URL"]
    work_key = f"reaper-idempotent-{uuid.uuid4().hex}"
    _insert_claim(conninfo, work_key, state="CLAIMED", lease_offset_seconds=-5)

    first = reaper.handler({}, None)
    assert _fetch_state(conninfo, work_key) == "ABANDONED"

    second = reaper.handler({}, None)
    # The row is already ABANDONED, so a second sweep must not re-select
    # it — reaped_count from the second call proves nothing about THIS
    # row specifically, but the state must be unchanged.
    assert _fetch_state(conninfo, work_key) == "ABANDONED"
    assert isinstance(second["reaped_count"], int)
    assert first["reaped_count"] >= 1


def test_reaper_sweeps_twice_and_catches_a_lease_expiring_between_sweeps(pool) -> None:
    # EventBridge can't schedule sub-minute, so one Lambda invocation does
    # two sweeps GAP_SECONDS apart to keep a real 30s cadence — a lease
    # that's still live at sweep 1 but expires before sweep 2 must still
    # get reaped within this same invocation, not wait for the next one.
    conninfo = os.environ["CAIRN_DATABASE_URL"]
    work_key = f"reaper-between-sweeps-{uuid.uuid4().hex}"
    gap = float(os.environ["REAPER_SWEEP_GAP_SECONDS"])
    # Alive when sweep 1 runs, expired by the time sweep 2 runs.
    _insert_claim(conninfo, work_key, state="CLAIMED", lease_offset_seconds=gap / 2)

    result = reaper.handler({}, None)

    assert result["reaped_count"] >= 1
    assert _fetch_state(conninfo, work_key) == "ABANDONED"
