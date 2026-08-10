"""Day 7 gate: contradiction quarantines, advances rule head, next plan refuses.

Appendix E:
  uv run pytest tests/integration/test_contradiction_tightening.py -q

A failed probe quarantines the derivation, advances the rule head, and the
next plan refuses the formerly valid identity shortcut. Similarity never
authorizes.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from cairn.db import flight as flight_db
from cairn.db.txn import in_txn
from cairn.flight.contracts import (
    CONTRACT_JSONL_MAP,
    build_execution_spec,
    jsonl_map_adapter_contract,
)
from cairn.flight.executor import plan_execution
from cairn.flight.types import (
    AccessMode,
    ObservationSource,
    PlanAction,
    PlatformContract,
    ResourceIdentity,
    ResourceKind,
)

pytestmark = pytest.mark.integration


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _seed_published_derivation(
    pool,
    *,
    namespace_id: str,
    semantic_work_key: str,
    rule: flight_db.RuleRevisionRef,
) -> uuid.UUID:
    derivation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    blob_digest = _digest(f"blob-{derivation_id}")
    claim_key = _digest(f"claim-{namespace_id}-{semantic_work_key}-1")

    def _tx(cur):  # type: ignore[no-untyped-def]
        cur.execute(
            """
            INSERT INTO namespaces (namespace_id, display_name)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
            (namespace_id, "contradiction-test"),
        )
        cur.execute(
            """
            INSERT INTO environments
              (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (_digest("env"), "sha256:test", "3.12.0", Jsonb({}), 1),
        )
        cur.execute(
            """
            INSERT INTO runs (run_id, work_key, stage, state, region, started_at)
            VALUES (%s, %s, 'exec', 'SUCCEEDED', 'us-east-1', now())
            ON CONFLICT DO NOTHING
            """,
            (run_id, claim_key),
        )
        cur.execute(
            """
            INSERT INTO content_blobs
              (blob_digest, bucket, object_key, version_id, checksum_sha256,
               size_bytes, canonicalization_version, integrity_state)
            VALUES (%s, 'test-bucket', %s, 'v1', %s, 12, 'cas/v1', 'VALID')
            ON CONFLICT DO NOTHING
            """,
            (blob_digest, f"cas/sha256/{blob_digest}", blob_digest),
        )
        cur.execute(
            """
            INSERT INTO work_claims
              (work_key, stage, state, owner_id, owner_host, owner_region,
               fence, lease_expires_at, run_id, derivation_id)
            VALUES (%s, 'exec', 'SUCCEEDED', 'test-owner', 'host', 'us-east-1',
                    1, now() + interval '1 hour', %s, %s)
            ON CONFLICT (work_key) DO UPDATE
              SET state = 'SUCCEEDED', derivation_id = EXCLUDED.derivation_id
            """,
            (claim_key, run_id, derivation_id),
        )
        cur.execute(
            """
            INSERT INTO work_generations
              (namespace_id, semantic_work_key, generation, claim_key, lifecycle_state,
               current_derivation_id)
            VALUES (%s, %s, 1, %s, 'PUBLISHED', NULL)
            ON CONFLICT DO NOTHING
            """,
            (namespace_id, semantic_work_key, claim_key),
        )
        cur.execute(
            """
            INSERT INTO work_heads (namespace_id, semantic_work_key, current_generation)
            VALUES (%s, %s, 1)
            ON CONFLICT (namespace_id, semantic_work_key)
            DO UPDATE SET current_generation = 1
            """,
            (namespace_id, semantic_work_key),
        )
        cur.execute(
            """
            INSERT INTO derivations
              (derivation_id, namespace_id, semantic_work_key, generation, blob_digest,
               observation_id, produced_by_run, committed_fence, rule_id, rule_revision,
               state)
            VALUES (%s, %s, %s, 1, %s, NULL, %s, 1, %s, %s, 'PUBLISHED')
            """,
            (
                derivation_id,
                namespace_id,
                semantic_work_key,
                blob_digest,
                run_id,
                rule.rule_id,
                rule.revision,
            ),
        )
        cur.execute(
            """
            UPDATE work_generations
               SET current_derivation_id = %s
             WHERE namespace_id = %s AND semantic_work_key = %s AND generation = 1
            """,
            (derivation_id, namespace_id, semantic_work_key),
        )

    in_txn(pool, _tx, op="test.seed_contradiction_derivation")
    return derivation_id


def test_contradiction_tightening_refuses_former_shortcut(
    pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.environ.get("CAIRN_DATABASE_URL"):
        pytest.skip("CAIRN_DATABASE_URL not set")

    namespace_id = f"contradict-{uuid.uuid4().hex[:8]}"
    semantic_work_key = _digest(f"work-{namespace_id}")
    rule_id = f"flight/identity-restore/{namespace_id}"
    rule = flight_db.ensure_reuse_rule(
        pool, rule_id=rule_id, required_authority="identity", reason="day7 fixture"
    )
    derivation_id = _seed_published_derivation(
        pool,
        namespace_id=namespace_id,
        semantic_work_key=semantic_work_key,
        rule=rule,
    )

    before = flight_db.current_derivations(
        pool, namespace_id=namespace_id, keys=[semantic_work_key]
    )
    assert semantic_work_key in before
    assert before[semantic_work_key].derivation_id == derivation_id

    contradicting_run = uuid.uuid4()
    result = flight_db.contradict_and_tighten(
        pool,
        rule_id=rule_id,
        derivation_id=derivation_id,
        namespace_id=namespace_id,
        semantic_work_key=semantic_work_key,
        contradicting_run=contradicting_run,
        evidence="probe failed: output digest mismatch under identity authority",
        new_required_authority="probe",
    )
    assert result.rule.revision == rule.revision + 1
    assert result.required_authority == "probe"
    assert result.new_generation == 2

    after = flight_db.current_derivations(
        pool, namespace_id=namespace_id, keys=[semantic_work_key]
    )
    assert semantic_work_key not in after

    head = flight_db.current_rule_authority(pool, rule_id=rule_id)
    assert head is not None
    assert head[0] == result.rule.revision
    assert head[1] == "TIGHTENED"
    assert head[2] == "probe"

    from cairn.flight import executor as executor_mod
    from cairn.flight.executor import _tightened_rule_refuse_reason

    refuse = _tightened_rule_refuse_reason(
        pool, namespace_id=namespace_id, work=semantic_work_key
    )
    assert refuse is not None
    assert "similarity" not in refuse.lower()
    assert any(token in refuse for token in ("probe", "tightened", "contradiction", "quarantined"))

    # Force planner onto the seeded key so the refuse path is exercised end-to-end.
    monkeypatch.setattr(executor_mod, "compute_semantic_key", lambda *_a, **_k: semantic_work_key)

    workspace = tmp_path
    input_path = workspace / "in.jsonl"
    input_path.write_text('{"id":"1"}\n', encoding="utf-8")
    declared = (
        ResourceIdentity(
            kind=ResourceKind.FILE,
            ref="in.jsonl",
            access_mode=AccessMode.READ,
            exists=True,
            version_digest=_digest("in.jsonl"),
            resolver="workspace_file/v1",
            source=ObservationSource.DECLARED,
        ),
    )
    spec = build_execution_spec(
        namespace_id=namespace_id,
        argv=("python", "examples/embed_mapper.py"),
        cwd_rel=".",
        output_rel="out/root.jsonl",
        contract_id=CONTRACT_JSONL_MAP,
        platform=PlatformContract(
            os="linux",
            arch="amd64",
            image_digest="sha256:" + "a" * 64,
        ),
        adapter=jsonl_map_adapter_contract(
            mapper_path=Path("examples/embed_mapper.py"), partitions=8
        ),
    )
    plan = plan_execution(
        pool,
        spec=spec,
        workspace=workspace,
        owner_id="test-owner",
        host="testhost",
        region="us-east-1",
        run_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        declared_inputs=declared,
        allow_coalesce=False,
    )
    assert plan.action is not PlanAction.RESTORE
    assert plan.semantic_work_key == semantic_work_key
    assert plan.action is PlanAction.REFUSE_REUSE
    assert "similarity" not in plan.reason.lower()
    assert any(
        token in plan.reason for token in ("probe", "tightened", "contradiction", "quarantined")
    )
