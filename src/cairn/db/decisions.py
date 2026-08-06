"""Transactional persistence for the decision ledger and probe evidence —
D5, PROJECT.md §3.1 and §6.4.

"The model may propose reuse. Deterministic evidence must authorize it."
`db/migrations/0005_decisions.sql`'s CHECK constraint makes a
model-authorized reuse unrepresentable at the schema level — there is no
enum value `'model'` in `authorized_by` at all. `ReuseDecision.__post_init__`
enforces the same rule in Python, before a single round-trip to the
database: the CHECK is the last line of defense, not the first, and the
integration test in `tests/integration/test_decisions.py` proves it still
holds even if this module's own guard were ever bypassed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.probes.base import ProbeResult

AUTHORIZED_BY = frozenset({"probe", "structural", "identity"})
PROPOSED_BY = frozenset({"rule", "model"})
# The nine actions, PROJECT.md §6.4.
ACTIONS = frozenset(
    {
        "REUSE",
        "PARTIAL_REUSE",
        "RECOMPUTE",
        "REFUSE_DUPLICATE",
        "SUBSCRIBE",
        "REFUSE_DOOMED",
        "REMEDIATE_AND_REPLAN",
        "RESUME",
        "ESCALATE",
    }
)
VERDICTS = frozenset({"reuse", "recompute", "refused", "subscribed", "resumed"})


class InvalidDecision(ValueError):
    """Raised before the DB is even asked — see module docstring."""


@dataclass(frozen=True)
class ReuseDecision:
    work_key: str
    stage: str
    action: str
    verdict: str
    proposed_by: str
    latency_ms: int
    explanation: str
    change_class: str | None = None
    model_id: str | None = None
    authorized_by: str | None = None
    probe_run_id: uuid.UUID | None = None
    candidate_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise InvalidDecision(f"action must be one of {sorted(ACTIONS)}, got {self.action!r}")
        if self.verdict not in VERDICTS:
            raise InvalidDecision(
                f"verdict must be one of {sorted(VERDICTS)}, got {self.verdict!r}"
            )
        if self.proposed_by not in PROPOSED_BY:
            raise InvalidDecision(f"proposed_by must be one of {sorted(PROPOSED_BY)}")
        if self.authorized_by is not None and self.authorized_by not in AUTHORIZED_BY:
            raise InvalidDecision(f"authorized_by must be one of {sorted(AUTHORIZED_BY)} or None")
        if self.verdict == "reuse" and self.authorized_by not in AUTHORIZED_BY:
            raise InvalidDecision(
                "verdict='reuse' requires authorized_by in "
                f"{sorted(AUTHORIZED_BY)} — the model may propose reuse, "
                "deterministic evidence must authorize it (PROJECT.md §3.1)"
            )
        if self.latency_ms < 0:
            raise InvalidDecision("latency_ms must be non-negative")


def insert_probe_run(
    cur: psycopg.Cursor, result: ProbeResult, *, artifact_id: str | None
) -> uuid.UUID:
    """Cursor-level write, composable inside a caller's own transaction —
    mirrors db/graph.py's insert_artifact so a probe run and the decision
    that relies on it can be recorded in one atomic commit when that
    matters to the caller."""

    probe_run_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO probe_runs
          (probe_run_id, probe_type, artifact_id, sample_spec, population_size,
           sample_size, tolerance, runtime_ms, passed, evidence_digest, detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            probe_run_id,
            result.probe_type,
            artifact_id,
            result.sample_spec,
            result.population_size,
            result.sample_size,
            result.tolerance,
            result.runtime_ms,
            result.passed,
            result.evidence_digest,
            result.detail,
        ),
    )
    return probe_run_id


def insert_decision(cur: psycopg.Cursor, decision: ReuseDecision) -> uuid.UUID:
    """Cursor-level write — see insert_probe_run's docstring."""

    decision_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO reuse_decisions
          (decision_id, work_key, stage, action, verdict, change_class,
           proposed_by, model_id, authorized_by, probe_run_id,
           candidate_artifact_id, latency_ms, explanation)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            decision_id,
            decision.work_key,
            decision.stage,
            decision.action,
            decision.verdict,
            decision.change_class,
            decision.proposed_by,
            decision.model_id,
            decision.authorized_by,
            decision.probe_run_id,
            decision.candidate_artifact_id,
            decision.latency_ms,
            decision.explanation,
        ),
    )
    return decision_id


def record_probe_run(
    pool: ConnectionPool, result: ProbeResult, *, artifact_id: str | None = None
) -> uuid.UUID:
    def _tx(cur: psycopg.Cursor) -> uuid.UUID:
        return insert_probe_run(cur, result, artifact_id=artifact_id)

    return in_txn(pool, _tx, op="decisions.record_probe_run")


def record_decision(pool: ConnectionPool, decision: ReuseDecision) -> uuid.UUID:
    def _tx(cur: psycopg.Cursor) -> uuid.UUID:
        return insert_decision(cur, decision)

    return in_txn(pool, _tx, op="decisions.record_decision")


def record_decision_with_probe(
    pool: ConnectionPool,
    decision_without_probe_run_id: ReuseDecision,
    probe_result: ProbeResult,
    *,
    artifact_id: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Write the probe's evidence and the decision that cites it in one
    transaction — the common case, and the one PROJECT.md §3.1 cares
    about: a decision must never be visible without its authorizing
    evidence already being visible too."""

    if decision_without_probe_run_id.probe_run_id is not None:
        raise InvalidDecision(
            "decision_without_probe_run_id.probe_run_id must be None — "
            "this function assigns it from the probe write"
        )

    def _tx(cur: psycopg.Cursor) -> tuple[uuid.UUID, uuid.UUID]:
        probe_run_id = insert_probe_run(cur, probe_result, artifact_id=artifact_id)
        decision = ReuseDecision(
            work_key=decision_without_probe_run_id.work_key,
            stage=decision_without_probe_run_id.stage,
            action=decision_without_probe_run_id.action,
            verdict=decision_without_probe_run_id.verdict,
            proposed_by=decision_without_probe_run_id.proposed_by,
            latency_ms=decision_without_probe_run_id.latency_ms,
            explanation=decision_without_probe_run_id.explanation,
            change_class=decision_without_probe_run_id.change_class,
            model_id=decision_without_probe_run_id.model_id,
            authorized_by=decision_without_probe_run_id.authorized_by,
            probe_run_id=probe_run_id,
            candidate_artifact_id=decision_without_probe_run_id.candidate_artifact_id,
        )
        decision_id = insert_decision(cur, decision)
        return decision_id, probe_run_id

    return in_txn(pool, _tx, op="decisions.record_decision_with_probe")
