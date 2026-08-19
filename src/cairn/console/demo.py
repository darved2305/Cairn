"""Judge mode's "Run the demo" / "Reset demo" controls — docs/project/PROJECT.md §7.2.

**What this actually is, stated plainly, because the distinction matters:**
this is a *replay* of rows that are already in the cluster, paced by the
timings those rows recorded. It is not a re-execution. Pressing "Run the
demo" does not launch an ECS task, does not write to CockroachDB, and does
not spend a cent — the console is read-only by construction (`console/
queries.py` is all SELECTs, and once `db/migrations/0008` is wired it holds a
SQL role that cannot write even if the code tried).

That is a deliberate scoping decision, not an oversight. Real re-execution of
docs/project/PROJECT.md §12's four scenarios means two-region `ecs:RunTask`, real S3
writes, and a real `docker kill` mid-`features` — a public, unauthenticated
URL that anyone can point at real compute is a cost and abuse problem, and
the console has no credential to do it with anyway. The honest version is
this: replay the recorded run at its recorded pace, and label it as a replay
everywhere it is shown. `make demo` (`cairn run --all`) remains the thing
that actually executes the scenarios, from a terminal, by someone who meant
to.

Every step a replay emits is therefore derived from a real row — a
`reuse_decisions` verdict, an `ownership_transfers` fence bump, a
`failure_signatures` traceback — and carries `source_table` /
`source_id` so a judge can go check it. A scenario with no backing rows
reports itself as `unavailable` rather than inventing a story.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg
from psycopg_pool import ConnectionPool

from cairn.console.queries import _PRODUCT_ARTIFACT_FILTER, _PRODUCT_DECISION_FILTER
from cairn.db.txn import in_txn

# Wall-clock pacing. A recorded 95 s feature stage is not replayed at 95 s —
# docs/project/PROJECT.md §12's own video does the same thing and labels it. The factor is
# reported in the API response so the number on screen is never mistaken for
# a live measurement.
PLAYBACK_SPEED = 6.0
MIN_STEP_S = 0.6
MAX_STEP_S = 4.0


@dataclass(frozen=True)
class DemoStep:
    index: int
    scenario: str
    title: str
    detail: str
    panel: str
    recorded_ms: int
    dwell_s: float
    source_table: str
    source_id: str


@dataclass(frozen=True)
class DemoScenario:
    key: str
    title: str
    proves: str
    available: bool
    unavailable_reason: str | None
    steps: list[DemoStep]


@dataclass
class DemoSession:
    demo_run_id: uuid.UUID
    started_monotonic: float
    scenarios: list[DemoScenario]
    steps: list[DemoStep] = field(default_factory=list)

    def total_s(self) -> float:
        return sum(step.dwell_s for step in self.steps)


_LOCK = threading.Lock()
_SESSION: DemoSession | None = None


def _dwell(recorded_ms: int) -> float:
    seconds = (recorded_ms / 1000.0) / PLAYBACK_SPEED
    return round(min(MAX_STEP_S, max(MIN_STEP_S, seconds)), 2)


def _build(pool: ConnectionPool) -> list[DemoScenario]:
    """Assemble the four scenarios from whatever the cluster actually holds."""

    def _tx(cur: psycopg.Cursor) -> list[DemoScenario]:
        index = 0

        def _step(
            scenario: str,
            title: str,
            detail: str,
            panel: str,
            recorded_ms: int,
            source_table: str,
            source_id: str,
        ) -> DemoStep:
            nonlocal index
            step = DemoStep(
                index=index,
                scenario=scenario,
                title=title,
                detail=detail,
                panel=panel,
                recorded_ms=recorded_ms,
                dwell_s=_dwell(recorded_ms),
                source_table=source_table,
                source_id=source_id,
            )
            index += 1
            return step

        scenarios: list[DemoScenario] = []

        # --- 1. Evidence-backed reuse (docs/project/PROJECT.md §4.4, video 0:15-0:45) ---
        cur.execute(
            f"""
            SELECT d.decision_id, d.stage, d.action, d.verdict, d.authorized_by,
                   d.latency_ms, d.explanation, p.probe_type, p.sample_size, p.population_size,
                   p.tolerance, p.runtime_ms
              FROM reuse_decisions d
              LEFT JOIN probe_runs p ON p.probe_run_id = d.probe_run_id
             WHERE d.verdict = 'reuse' AND {_PRODUCT_DECISION_FILTER}
             ORDER BY d.created_at DESC
             LIMIT 5
            """
        )
        reuse_rows = cur.fetchall()
        reuse_steps = [
            _step(
                "evidence_backed_reuse",
                f"{row[1]}: {row[3]} authorized by {row[4] or 'unrecorded'}",
                (
                    f"{row[6]} — probe {row[7]} on {row[8]}/{row[9]} samples, "
                    f"tolerance {row[10]}, {row[11]} ms"
                    if row[7] is not None
                    else row[6]
                ),
                "causal_graph",
                int(row[5]),
                "reuse_decisions",
                str(row[0]),
            )
            for row in reuse_rows
        ]
        scenarios.append(
            DemoScenario(
                key="evidence_backed_reuse",
                title="Evidence-backed reuse",
                proves="A probe or a structural proof authorized reuse — never the model.",
                available=bool(reuse_steps),
                unavailable_reason=(
                    None if reuse_steps else "no verdict='reuse' rows in reuse_decisions yet"
                ),
                steps=reuse_steps,
            )
        )

        # --- 2. Causal partial reuse (docs/project/PROJECT.md §4.3, video 0:45-1:20) ---
        cur.execute(
            f"""
            SELECT d.decision_id, d.stage, d.action, d.verdict, d.latency_ms, d.explanation
              FROM reuse_decisions d
             WHERE d.verdict IN ('reuse', 'recompute') AND {_PRODUCT_DECISION_FILTER}
             ORDER BY d.created_at DESC
             LIMIT 10
            """
        )
        mixed = cur.fetchall()
        has_both = {row[3] for row in mixed} == {"reuse", "recompute"}
        partial_steps = (
            [
                _step(
                    "causal_partial_reuse",
                    f"{row[1]}: {row[3]}",
                    row[5],
                    "causal_graph",
                    int(row[4]),
                    "reuse_decisions",
                    str(row[0]),
                )
                for row in mixed
            ]
            if has_both
            else []
        )
        scenarios.append(
            DemoScenario(
                key="causal_partial_reuse",
                title="Causal partial reuse",
                proves="The expensive stage survives a change that cannot reach it.",
                available=bool(partial_steps),
                unavailable_reason=(
                    None
                    if partial_steps
                    else "needs both a reuse and a recompute verdict on record to be a "
                    "partial-reuse story; the ledger currently has only one kind"
                ),
                steps=partial_steps,
            )
        )

        # --- 3. Claim race, fencing, crash resume (docs/project/PROJECT.md §4.2/§4.5) ---
        cur.execute(
            """
            SELECT t.transfer_id, t.work_key, t.from_owner, t.to_owner,
                   t.from_fence, t.to_fence, t.reason
              FROM ownership_transfers t
              JOIN work_claims c ON c.work_key = t.work_key
             WHERE c.stage <> 'race_test'
             ORDER BY t.at DESC
             LIMIT 6
            """
        )
        transfers = cur.fetchall()
        race_steps = [
            _step(
                "claim_race",
                f"fence {row[4]} -> {row[5]} on {row[1][:12]}...",
                f"{row[2]} lost the lease to {row[3]} ({row[6]}); the stale owner's next "
                f"fenced write updates zero rows and it exits.",
                "claim_theatre",
                1200,
                "ownership_transfers",
                str(row[0]),
            )
            for row in transfers
        ]
        cur.execute(
            f"""
            SELECT work_key, count(*), max(fragment_index), coalesce(sum(duration_ms), 0)
              FROM run_fragments
             WHERE {_PRODUCT_ARTIFACT_FILTER}
             GROUP BY work_key ORDER BY 2 DESC LIMIT 2
            """
        )
        for work_key, count, latest, total_ms in cur.fetchall():
            race_steps.append(
                _step(
                    "claim_race",
                    f"resumed from fragment {latest} on {work_key[:12]}...",
                    f"{count} fragment(s) verified against S3 before resuming; "
                    f"{total_ms} ms of recorded fragment work not repeated.",
                    "claim_theatre",
                    int(total_ms),
                    "run_fragments",
                    work_key,
                )
            )
        scenarios.append(
            DemoScenario(
                key="claim_race",
                title="Distributed claim race and crash resume",
                proves="Serializable arbitration, fencing, and resume from recorded fragments.",
                available=bool(race_steps),
                unavailable_reason=(
                    None
                    if race_steps
                    else "no ownership_transfers or run_fragments rows — run `make race` "
                    "or a killed worker against this cluster first"
                ),
                steps=race_steps,
            )
        )

        # --- 4. Negative memory: refuse -> remediate -> re-plan (§4.1) ---
        cur.execute(
            """
            SELECT fs.signature_id, fs.stage, fs.error_class, fs.traceback_head,
                   fs.wasted_ms, r.rationale, r.succeeded
              FROM failure_signatures fs
              LEFT JOIN LATERAL (
                     SELECT rationale, succeeded FROM remediations
                      WHERE remediations.signature_id = fs.signature_id
                      ORDER BY succeeded DESC, created_at DESC LIMIT 1
                   ) r ON true
             ORDER BY fs.created_at DESC
             LIMIT 4
            """
        )
        memory_steps = []
        for sig_id, stage, error_class, head, wasted_ms, rationale, succeeded in cur.fetchall():
            memory_steps.append(
                _step(
                    "negative_memory",
                    f"{stage}: remembered {error_class}",
                    head,
                    "negative_memory",
                    int(wasted_ms),
                    "failure_signatures",
                    str(sig_id),
                )
            )
            if rationale:
                memory_steps.append(
                    _step(
                        "negative_memory",
                        "remediation on record"
                        + (" (verified by a real run)" if succeeded else " (unverified proposal)"),
                        rationale,
                        "negative_memory",
                        800,
                        "remediations",
                        str(sig_id),
                    )
                )
        cur.execute(
            f"""
            SELECT d.decision_id, d.stage, d.explanation, d.latency_ms
              FROM reuse_decisions d
             WHERE d.action IN ('REFUSE_DOOMED', 'REMEDIATE_AND_REPLAN')
               AND {_PRODUCT_DECISION_FILTER}
             ORDER BY d.created_at DESC LIMIT 3
            """
        )
        for decision_id, stage, explanation, latency_ms in cur.fetchall():
            memory_steps.append(
                _step(
                    "negative_memory",
                    f"{stage}: plan refused before any claim was taken",
                    explanation,
                    "decision_ledger",
                    int(latency_ms),
                    "reuse_decisions",
                    str(decision_id),
                )
            )
        scenarios.append(
            DemoScenario(
                key="negative_memory",
                title="Negative memory: refuse, remediate, re-plan",
                proves="A failure that already happened once does not get paid for twice.",
                available=bool(memory_steps),
                unavailable_reason=(
                    None if memory_steps else "no failure_signatures rows — run `make seed` first"
                ),
                steps=memory_steps,
            )
        )
        return scenarios

    return in_txn(pool, _tx, op="console.demo_build")


def start(pool: ConnectionPool) -> DemoSession:
    global _SESSION
    scenarios = _build(pool)
    steps = [step for scenario in scenarios for step in scenario.steps]
    with _LOCK:
        _SESSION = DemoSession(
            demo_run_id=uuid.uuid4(),
            started_monotonic=time.monotonic(),
            scenarios=scenarios,
            steps=steps,
        )
        return _SESSION


def reset() -> None:
    """Restore the seeded, at-rest view. Nothing is rolled back in the
    database because nothing was written to it — this drops the in-process
    replay session, and the panels go back to reading the cluster live."""

    global _SESSION
    with _LOCK:
        _SESSION = None


def state() -> dict[str, object]:
    with _LOCK:
        session = _SESSION
    if session is None:
        return {"running": False, "demo_run_id": None, "elapsed_s": 0.0, "played": []}

    elapsed = time.monotonic() - session.started_monotonic
    played: list[int] = []
    cursor = 0.0
    current: int | None = None
    for step in session.steps:
        if elapsed >= cursor + step.dwell_s:
            played.append(step.index)
        elif elapsed >= cursor:
            current = step.index
        cursor += step.dwell_s
    return {
        "running": elapsed < session.total_s(),
        "demo_run_id": str(session.demo_run_id),
        "elapsed_s": round(elapsed, 2),
        "total_s": round(session.total_s(), 2),
        "played": played,
        "current": current,
    }
