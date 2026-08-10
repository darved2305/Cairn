"""Unit tests for Flight leaf-path explain — no live cluster required.

Action classification (COMPUTED vs RESTORE) is defined against persisted
``produced_by_run`` equality; these tests lock that contract and the
stable table/JSON shape so the CLI cannot silently drift.
"""

from __future__ import annotations

import uuid

from cairn.flight.explain import LeafPathEntry, LeafPathExplanation, format_leaf_path_table


def _explanation(*, same_run: bool) -> LeafPathExplanation:
    root_run = uuid.uuid4()
    leaf = LeafPathEntry(
        bucket=7,
        child_derivation_id=uuid.uuid4(),
        blob_digest="ab" * 32,
        input_slice_digest="cd" * 32,
        action="COMPUTED" if same_run else "RESTORE",
        authorized_by=None if same_run else "identity",
        owner_id="owner-1",
        fence=18,
        task_arn="arn:aws:ecs:us-east-1:123:task/abc",
        subscriber_count=2,
        state="PUBLISHED",
    )
    return LeafPathExplanation(
        kind="flight_leaf_path",
        derivation_id=uuid.uuid4(),
        namespace_id="ns-test",
        semantic_work_key="ee" * 32,
        generation=1,
        blob_digest="ff" * 32,
        merkle_root_digest="11" * 32,
        verifier_digest="22" * 32,
        leaf_count=64,
        row_count=40,
        reused_leaves=63 if not same_run else 0,
        computed_leaves=1 if not same_run else 64,
        produced_by_run=root_run,
        committed_fence=18,
        state="PUBLISHED",
        leaves=(leaf,),
    )


def test_restore_leaf_is_identity_authorized() -> None:
    expl = _explanation(same_run=False)
    leaf = expl.leaves[0]
    assert leaf.action == "RESTORE"
    assert leaf.authorized_by == "identity"
    assert "authorized_by=identity" in format_leaf_path_table(expl)
    assert "RESTORE" in format_leaf_path_table(expl)


def test_computed_leaf_has_no_authorized_by() -> None:
    expl = _explanation(same_run=True)
    leaf = expl.leaves[0]
    assert leaf.action == "COMPUTED"
    assert leaf.authorized_by is None
    payload = expl.as_dict()
    assert payload["kind"] == "flight_leaf_path"
    assert payload["leaves"][0]["action"] == "COMPUTED"
    assert payload["leaves"][0]["authorized_by"] is None


def test_leaf_path_json_is_canonical_stable() -> None:
    expl = _explanation(same_run=False)
    first = expl.canonical_json()
    second = expl.canonical_json()
    assert first == second
    assert b"flight_leaf_path" in first
    assert b"identity" in first
