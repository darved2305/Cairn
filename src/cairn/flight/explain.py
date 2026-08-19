"""Flight leaf-path explanation — docs/project/PLAN.md §19 Day 6 / feature #28.

``cairn explain --run|--artifact|--work`` renders the persisted causal path
for a jsonl-map/v1 root: bucket → slice digest → leaf action → owner /
fence / task ARN → root verifier. Every field is read from CockroachDB
rows that ``publish_derivation`` / ``derivation_fragments`` already wrote;
this module never reconstructs a leaf decision from logs or heuristics.

Leaf action evidence: a leaf whose ``produced_by_run`` equals the root's
``produced_by_run`` was published in the same assembly run (COMPUTED);
otherwise the fragment points at a prior durable child (RESTORE). That is
the same distinction the leaf pipeline records when it chooses RESTORE vs
publish, not a guess after the fact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn
from cairn.fingerprint.canon import canonical_json


class ExplainNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class LeafPathEntry:
    bucket: int
    child_derivation_id: uuid.UUID
    blob_digest: str
    input_slice_digest: str
    action: str  # RESTORE | COMPUTED
    authorized_by: str | None
    owner_id: str | None
    fence: int | None
    task_arn: str | None
    subscriber_count: int
    state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "bucket": self.bucket,
            "child_derivation_id": str(self.child_derivation_id),
            "blob_digest": self.blob_digest,
            "input_slice_digest": self.input_slice_digest,
            "action": self.action,
            "authorized_by": self.authorized_by,
            "owner_id": self.owner_id,
            "fence": self.fence,
            "task_arn": self.task_arn,
            "subscriber_count": self.subscriber_count,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class LeafPathExplanation:
    """Root + leaf path for one published jsonl-map/v1 composite derivation."""

    kind: str  # "flight_leaf_path"
    derivation_id: uuid.UUID
    namespace_id: str
    semantic_work_key: str
    generation: int
    blob_digest: str
    merkle_root_digest: str | None
    verifier_digest: str | None
    leaf_count: int | None
    row_count: int | None
    reused_leaves: int | None
    computed_leaves: int | None
    produced_by_run: uuid.UUID
    committed_fence: int
    state: str
    leaves: tuple[LeafPathEntry, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "derivation_id": str(self.derivation_id),
            "namespace_id": self.namespace_id,
            "semantic_work_key": self.semantic_work_key,
            "generation": self.generation,
            "blob_digest": self.blob_digest,
            "merkle_root_digest": self.merkle_root_digest,
            "verifier_digest": self.verifier_digest,
            "leaf_count": self.leaf_count,
            "row_count": self.row_count,
            "reused_leaves": self.reused_leaves,
            "computed_leaves": self.computed_leaves,
            "produced_by_run": str(self.produced_by_run),
            "committed_fence": self.committed_fence,
            "state": self.state,
            "leaves": [leaf.as_dict() for leaf in self.leaves],
        }

    def canonical_json(self) -> bytes:
        return canonical_json(self.as_dict())


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _load_leaf_path(cur: psycopg.Cursor, derivation_id: uuid.UUID) -> LeafPathExplanation | None:
    cur.execute(
        """
        SELECT d.derivation_id, d.namespace_id, d.semantic_work_key, d.generation,
               d.blob_digest, d.state, d.produced_by_run, d.committed_fence,
               c.merkle_root_digest, c.verifier_digest, c.leaf_count, c.output_metadata
          FROM derivations d
          LEFT JOIN composite_derivations c ON c.parent_derivation_id = d.derivation_id
         WHERE d.derivation_id = %s
        """,
        (derivation_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    (
        der_id,
        namespace_id,
        semantic_work_key,
        generation,
        blob_digest,
        state,
        produced_by_run,
        committed_fence,
        merkle_root,
        verifier_digest,
        leaf_count,
        output_metadata,
    ) = row
    meta = output_metadata if isinstance(output_metadata, dict) else {}

    cur.execute(
        """
        SELECT f.ordinal, f.child_derivation_id, f.input_slice_digest,
               cd.blob_digest, cd.state, cd.produced_by_run,
               wc.owner_id, wc.fence, r.task_arn,
               (
                 SELECT count(*)::int
                   FROM work_subscribers ws
                  WHERE ws.namespace_id = cd.namespace_id
                    AND ws.semantic_work_key = cd.semantic_work_key
                    AND ws.generation = cd.generation
               ) AS subscriber_count
          FROM derivation_fragments f
          JOIN derivations cd ON cd.derivation_id = f.child_derivation_id
          LEFT JOIN work_generations g
            ON g.namespace_id = cd.namespace_id
           AND g.semantic_work_key = cd.semantic_work_key
           AND g.generation = cd.generation
          LEFT JOIN work_claims wc ON wc.work_key = g.claim_key
          LEFT JOIN runs r ON r.run_id = cd.produced_by_run
         WHERE f.parent_derivation_id = %s
         ORDER BY f.ordinal
        """,
        (derivation_id,),
    )
    leaves: list[LeafPathEntry] = []
    for lr in cur.fetchall():
        leaf_run = lr[5]
        action = "COMPUTED" if leaf_run == produced_by_run else "RESTORE"
        authorized_by = "identity" if action == "RESTORE" else None
        leaves.append(
            LeafPathEntry(
                bucket=lr[0],
                child_derivation_id=lr[1],
                input_slice_digest=lr[2],
                blob_digest=lr[3],
                state=lr[4],
                action=action,
                authorized_by=authorized_by,
                owner_id=lr[6],
                fence=lr[7],
                task_arn=lr[8],
                subscriber_count=int(lr[9] or 0),
            )
        )

    return LeafPathExplanation(
        kind="flight_leaf_path",
        derivation_id=der_id,
        namespace_id=namespace_id,
        semantic_work_key=semantic_work_key,
        generation=generation,
        blob_digest=blob_digest,
        merkle_root_digest=merkle_root,
        verifier_digest=verifier_digest,
        leaf_count=leaf_count,
        row_count=_as_int(meta.get("row_count")),
        reused_leaves=_as_int(meta.get("reused_leaves")),
        computed_leaves=_as_int(meta.get("computed_leaves")),
        produced_by_run=produced_by_run,
        committed_fence=committed_fence,
        state=state,
        leaves=tuple(leaves),
    )


def explain_by_derivation(pool: ConnectionPool, *, derivation_id: uuid.UUID) -> LeafPathExplanation:
    def _tx(cur: psycopg.Cursor) -> LeafPathExplanation | None:
        return _load_leaf_path(cur, derivation_id)

    result = in_txn(pool, _tx, op="flight.explain_by_derivation")
    if result is None:
        raise ExplainNotFound(f"no derivation with id={derivation_id}")
    return result


def explain_by_work_key(pool: ConnectionPool, *, semantic_work_key: str) -> LeafPathExplanation:
    """Current published composite root for ``semantic_work_key``, if any."""

    def _tx(cur: psycopg.Cursor) -> LeafPathExplanation | None:
        cur.execute(
            """
            SELECT d.derivation_id
              FROM derivations d
              JOIN composite_derivations c ON c.parent_derivation_id = d.derivation_id
             WHERE d.semantic_work_key = %s AND d.state = 'PUBLISHED'
             ORDER BY d.created_at DESC
             LIMIT 1
            """,
            (semantic_work_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _load_leaf_path(cur, row[0])

    result = in_txn(pool, _tx, op="flight.explain_by_work_key")
    if result is None:
        raise ExplainNotFound(f"no published composite derivation for work={semantic_work_key!r}")
    return result


def format_leaf_path_table(explanation: LeafPathExplanation) -> str:
    lines: list[str] = [
        f"flight leaf path · {explanation.derivation_id}",
        "",
        f"namespace          {explanation.namespace_id}",
        f"semantic_work_key  {explanation.semantic_work_key}",
        f"generation         {explanation.generation}",
        f"state              {explanation.state}",
        f"blob_digest        {explanation.blob_digest}",
        f"merkle_root        {explanation.merkle_root_digest or '-'}",
        f"verifier           {explanation.verifier_digest or '-'}",
        f"committed_fence    {explanation.committed_fence}",
        f"leaf_count         {explanation.leaf_count if explanation.leaf_count is not None else '-'}",
        f"reused_leaves      {explanation.reused_leaves if explanation.reused_leaves is not None else '-'}",
        f"computed_leaves    {explanation.computed_leaves if explanation.computed_leaves is not None else '-'}",
        "",
        "leaves",
    ]
    if not explanation.leaves:
        lines.append("  (none — not a composite root, or fragments missing)")
    for leaf in explanation.leaves:
        auth = leaf.authorized_by or "-"
        lines.append(
            f"  bucket={leaf.bucket:<3} {leaf.action:<8} authorized_by={auth:<8} "
            f"fence={leaf.fence if leaf.fence is not None else '-'} "
            f"subs={leaf.subscriber_count} "
            f"slice={leaf.input_slice_digest[:16]}… "
            f"blob={leaf.blob_digest[:16]}…"
        )
        if leaf.owner_id or leaf.task_arn:
            lines.append(
                f"             owner={leaf.owner_id or '-'}  task_arn={leaf.task_arn or '-'}"
            )
    return "\n".join(lines)
