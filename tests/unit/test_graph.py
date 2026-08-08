from __future__ import annotations

import uuid

import pytest

from cairn.db.claims import ArtifactRecord
from cairn.db.graph import ArtifactInput, insert_artifact


class Cursor:
    def __init__(self, *, conflict: bool = False) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._conflict = conflict

    def execute(self, query: str, params: tuple) -> None:
        self.statements.append((query, params))

    def fetchone(self):
        if self._conflict:
            return None
        return (self.statements[-1][1][-1],)


def _artifact() -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="artifact",
        stage="eval",
        work_key="work",
        s3_uri="s3://bucket/artifact",
        size_bytes=10,
        env_fingerprint="env",
        produced_by_run=uuid.uuid4(),
        duration_ms=5,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )


def test_insert_artifact_writes_node_then_sorted_typed_edges() -> None:
    cursor = Cursor()
    insert_artifact(
        cursor,  # type: ignore[arg-type]
        _artifact(),
        [ArtifactInput("upstream", "z", "z"), ArtifactInput("config", "eval.metrics", "m")],
    )
    assert "INSERT INTO artifacts" in cursor.statements[0][0]
    assert cursor.statements[1][1][1:3] == ("config", "eval.metrics")
    assert cursor.statements[2][1][1:3] == ("upstream", "z")


def test_duplicate_input_identity_is_rejected_before_any_sql() -> None:
    cursor = Cursor()
    duplicate = [ArtifactInput("config", "x", "1"), ArtifactInput("config", "x", "1")]
    with pytest.raises(ValueError, match="duplicate"):
        insert_artifact(cursor, _artifact(), duplicate)  # type: ignore[arg-type]
    assert cursor.statements == []


def test_conflicting_existing_provenance_is_rejected() -> None:
    from cairn.db.graph import CausalInputConflict

    cursor = Cursor(conflict=True)
    with pytest.raises(CausalInputConflict, match="different digest"):
        insert_artifact(  # type: ignore[arg-type]
            cursor, _artifact(), [ArtifactInput("config", "eval.metrics", "new")]
        )


def test_claim_completion_can_converge_on_existing_content_address() -> None:
    cursor = Cursor(conflict=True)
    insert_artifact(
        cursor,  # type: ignore[arg-type]
        _artifact(),
        [ArtifactInput("env", "environment", "different-env")],
        allow_existing=True,
    )
    assert len(cursor.statements) == 1
    assert "ON CONFLICT (artifact_id) DO NOTHING" in cursor.statements[0][0]


@pytest.mark.parametrize("kind", ["", "model", "CONFIG"])
def test_unknown_input_kinds_are_rejected(kind: str) -> None:
    with pytest.raises(ValueError, match="input_kind"):
        ArtifactInput(kind, "ref", "digest")
