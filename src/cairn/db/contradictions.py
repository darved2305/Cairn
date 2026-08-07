"""Contradiction handling and one-way quarantine — PROJECT.md §6.5.

If a later run produces evidence that contradicts an earlier reuse, Cairn
writes a `contradictions` row, quarantines the reused artifact
(`artifacts.quarantined_at`), and transitively quarantines every artifact
downstream of it (walked via `artifact_inputs.input_kind='upstream'`) since
each of those was built on a foundation that turned out to be wrong. This
is the mechanism PROJECT.md §3.1 depends on: "the model may propose reuse"
is only survivable if a wrong deterministic authorization is discoverable
and stoppable after the fact.

Quarantine is one-way from here — `unquarantine` (the CLI command, D8) is
the only reversal path, and it writes its own audited row rather than
calling anything in this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.obs.events import emit_event
from cairn.obs.metrics import emit_metric


@dataclass(frozen=True)
class Contradiction:
    contradiction_id: uuid.UUID
    artifact_id: str
    contradicting_run: uuid.UUID
    evidence: str
    quarantined: bool
    created_at: datetime


def _downstream_of(cur: psycopg.Cursor, artifact_id: str) -> list[str]:
    """Artifacts whose `artifact_inputs` cites `artifact_id` as an
    'upstream' input — i.e. every artifact built directly on top of it.
    One hop only; the caller BFS-walks this to cover the full transitive
    closure, since `artifact_inputs.input_ref` for an upstream edge holds
    the referenced artifact_id directly (`planner.py::artifact_inputs`)."""

    cur.execute(
        """
        SELECT DISTINCT artifact_id FROM artifact_inputs
         WHERE input_kind = 'upstream' AND input_ref = %s
        """,
        (artifact_id,),
    )
    return [row[0] for row in cur.fetchall()]


def record_contradiction(
    pool: ConnectionPool,
    artifact_id: str,
    contradicting_run: uuid.UUID,
    evidence: str,
) -> Contradiction:
    """Write the contradiction, quarantine `artifact_id`, and transitively
    quarantine everything built on it — one transaction, so a reader can
    never observe a contradiction row whose quarantine hasn't taken effect
    yet, or a partially-quarantined downstream set."""

    def _tx(cur: psycopg.Cursor) -> tuple[Contradiction, int]:
        contradiction_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO contradictions
              (contradiction_id, artifact_id, contradicting_run, evidence, quarantined)
            VALUES (%s,%s,%s,%s,true)
            RETURNING created_at
            """,
            (contradiction_id, artifact_id, contradicting_run, evidence),
        )
        row = cur.fetchone()
        assert row is not None
        created_at = row[0]

        quarantined_count = 0
        frontier = [artifact_id]
        seen = {artifact_id}
        while frontier:
            current = frontier.pop()
            cur.execute(
                """
                UPDATE artifacts SET quarantined_at = now()
                 WHERE artifact_id = %s AND quarantined_at IS NULL
                RETURNING artifact_id
                """,
                (current,),
            )
            if cur.fetchone() is not None:
                quarantined_count += 1
            for downstream_id in _downstream_of(cur, current):
                if downstream_id not in seen:
                    seen.add(downstream_id)
                    frontier.append(downstream_id)

        return (
            Contradiction(
                contradiction_id=contradiction_id,
                artifact_id=artifact_id,
                contradicting_run=contradicting_run,
                evidence=evidence,
                quarantined=True,
                created_at=created_at,
            ),
            quarantined_count,
        )

    contradiction, quarantined_count = in_txn(pool, _tx, op="contradictions.record_contradiction")
    # Emitted after commit, deliberately outside the transaction (db/txn.py's
    # module docstring: no I/O inside `fn`) — a metric that under-reports by
    # one point on a replayed-then-committed attempt is harmless; a stdout
    # write inside a txn that gets retried would double-emit instead.
    emit_metric("QuarantineEvents", quarantined_count)
    emit_event(
        "artifact.quarantined",
        {
            "artifact_id": artifact_id,
            "contradiction_id": str(contradiction.contradiction_id),
            "evidence": evidence,
            "quarantined_count": quarantined_count,
        },
        run_id=str(contradicting_run),
    )
    return contradiction


def contradictions_for_artifact(pool: ConnectionPool, artifact_id: str) -> list[Contradiction]:
    """`cairn explain`'s contradiction-history section — every contradiction
    row (real quarantine events and their `unquarantine` reversals alike,
    see `unquarantine`'s own docstring) ever recorded against this exact
    artifact_id, newest first."""

    def _tx(cur: psycopg.Cursor) -> list[Contradiction]:
        cur.execute(
            """
            SELECT contradiction_id, artifact_id, contradicting_run, evidence,
                   quarantined, created_at
              FROM contradictions
             WHERE artifact_id = %s
             ORDER BY created_at DESC
            """,
            (artifact_id,),
        )
        return [Contradiction(*row) for row in cur.fetchall()]

    return in_txn(pool, _tx, op="contradictions.contradictions_for_artifact")


def unquarantine(pool: ConnectionPool, artifact_id: str, reason: str) -> bool:
    """Reverse quarantine on exactly one artifact — never transitive, unlike
    quarantining itself: an operator clearing artifact A because they've
    verified it's actually fine says nothing about whether B (built on A
    for other, still-bad reasons) is also fine. Returns False if the
    artifact wasn't quarantined to begin with."""

    if not reason:
        raise ValueError("reason must be non-empty")

    def _tx(cur: psycopg.Cursor) -> bool:
        cur.execute(
            """
            UPDATE artifacts SET quarantined_at = NULL
             WHERE artifact_id = %s AND quarantined_at IS NOT NULL
            RETURNING artifact_id
            """,
            (artifact_id,),
        )
        if cur.fetchone() is None:
            return False
        # contradictions.contradicting_run is NOT NULL and there is no
        # actual run behind a human's unquarantine decision — a fresh
        # gen_random_uuid() is a sentinel, not a fabricated run identity;
        # quarantined=false plus the "unquarantined:" evidence prefix is
        # what distinguishes this row from a real contradiction on replay.
        cur.execute(
            """
            INSERT INTO contradictions
              (artifact_id, contradicting_run, evidence, quarantined)
            VALUES (%s, gen_random_uuid(), %s, false)
            """,
            (artifact_id, f"unquarantined: {reason}"),
        )
        return True

    cleared = in_txn(pool, _tx, op="contradictions.unquarantine")
    if cleared:
        emit_event("artifact.unquarantined", {"artifact_id": artifact_id, "reason": reason})
    return cleared
