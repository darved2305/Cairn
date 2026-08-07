"""cairn claim-demo — the standalone claim-protocol process D7's ECS tasks
actually run for the cross-region race and takeover demos. Exercises the
real CLI entrypoint against a real cluster; the protocol itself is already
proven by tests/integration/test_claims.py, so this only checks the CLI
wiring (argument handling, output shape, environment bookkeeping) is
correct.

`claim_demo_command()` calls `close_pool()` on exit — correct for a real
standalone process, which is exactly what it emulates — but that closes
the module-global singleton `pool` (the fixture) yielded, and pytest
caches that same session-scoped object for the rest of the run. So every
query here goes through a freshly-fetched `get_pool()` (which reopens the
singleton if a prior CLI invocation closed it) rather than the `pool`
fixture value directly; `pool` is kept only as a parameter to get its
skip-if-no-CAIRN_DATABASE_URL behavior.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from typer.testing import CliRunner

from cairn.cli import app
from cairn.db import claims
from cairn.db.pool import get_pool
from cairn.db.txn import in_txn

pytestmark = pytest.mark.integration


def test_claim_demo_wins_and_completes_a_fresh_work_key(pool) -> None:
    work_key = f"cli-claim-demo-{uuid.uuid4().hex}"
    result = CliRunner().invoke(app, ["claim-demo", work_key, "--hold-seconds", "0"])
    assert result.exit_code == 0, result.output
    assert "WON fence=1" in result.output
    assert "COMPLETED ok=True" in result.output

    def _state(cur):
        cur.execute("SELECT state, artifact_id FROM work_claims WHERE work_key=%s", (work_key,))
        return cur.fetchone()

    state, artifact_id = in_txn(get_pool(), _state, op="test.claim_demo_state")
    assert state == "SUCCEEDED"
    assert artifact_id is not None


def test_claim_demo_reuses_an_already_succeeded_key(pool) -> None:
    work_key = f"cli-claim-demo-reuse-{uuid.uuid4().hex}"
    first = CliRunner().invoke(app, ["claim-demo", work_key, "--hold-seconds", "0"])
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(app, ["claim-demo", work_key, "--hold-seconds", "0"])
    assert second.exit_code == 0, second.output
    assert "REUSE artifact=" in second.output


def test_claim_demo_loser_subscribes_and_adopts(pool) -> None:
    # The winner's claim is left LIVE (CLAIMED, not completed) before
    # invoking the CLI — completing it upfront would hit the REUSE branch
    # instead (an identity match on an already-SUCCEEDED claim), which is
    # a different code path. SUBSCRIBE only applies while the other
    # worker's claim is still in progress, so the winner completes from a
    # background thread partway through the CLI's poll loop.
    work_key = f"cli-claim-demo-subscribe-{uuid.uuid4().hex}"
    owner_run_id = uuid.uuid4()
    live_pool = get_pool()
    winner = claims.acquire(
        live_pool, work_key, "race_driver", "other-owner", "host-a", "us-east-1", owner_run_id
    )
    assert winner.won
    artifact = claims.ArtifactRecord(
        artifact_id=f"artifact-{uuid.uuid4().hex}",
        stage="race_driver",
        work_key=work_key,
        s3_uri="s3://test/artifact",
        size_bytes=10,
        env_fingerprint="test-env",
        produced_by_run=owner_run_id,
        duration_ms=1,
        vcpu=2.0,
        mem_mib=4096,
        region="us-east-1",
    )

    def _insert_env(cur):
        cur.execute(
            "INSERT INTO environments (env_fingerprint, image_digest, python_version, deps, torch_threads) "
            "VALUES (%s,'sha256:test','3.12','{}',1) ON CONFLICT DO NOTHING",
            ("test-env",),
        )

    in_txn(live_pool, _insert_env, op="test.claim_demo_subscribe_env")

    def _complete_after_delay() -> None:
        time.sleep(1.5)
        assert claims.complete(live_pool, work_key, "other-owner", winner.fence, artifact)

    completer = threading.Thread(target=_complete_after_delay)
    completer.start()
    try:
        result = CliRunner().invoke(
            app, ["claim-demo", work_key, "--hold-seconds", "0", "--poll-interval-s", "0.5"]
        )
    finally:
        completer.join(timeout=10)

    assert result.exit_code == 0, result.output
    assert "SUBSCRIBE owner=other-owner" in result.output
    assert "ADOPTED state=SUCCEEDED" in result.output
