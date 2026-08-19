"""Shared fixtures for tests that need real external dependencies: a live
CockroachDB cluster, or the real MiniLM model.

Nothing in tests/integration/ mocks these — see docs/project/PLAN.md §5's anti-simulation
rule. If a dependency isn't available, these tests skip cleanly instead of
faking a result.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from psycopg_pool import ConnectionPool

from cairn.db.pool import close_pool, get_pool


@pytest.fixture(scope="session", autouse=True)
def _close_pool_at_session_end() -> Iterator[None]:
    yield
    close_pool()


@pytest.fixture
def pool() -> ConnectionPool:
    """Function-scoped on purpose, not session-scoped: some tests (e.g.
    tests/integration/test_cli_claim_demo.py) invoke `cairn` CLI commands
    that call close_pool() themselves — correct behavior for a real
    standalone process. A session-scoped fixture would cache that one
    now-closed ConnectionPool object and hand it to every later test in
    the same pytest run (a real PoolClosed failure this exact bug once
    produced, run the full suite to see it recur if this reverts).
    get_pool() is a cheap lazy singleton — calling it fresh per test costs
    nothing when the pool is already open, and transparently reopens one
    if a prior test closed it.
    """
    if not os.environ.get("CAIRN_DATABASE_URL"):
        pytest.skip(
            "CAIRN_DATABASE_URL not set — integration tests need a live "
            "CockroachDB cluster (see scripts/provision_cluster.sh)"
        )
    return get_pool()


@pytest.fixture(scope="session")
def warm_embedding_model() -> None:
    """Load (and HF-cache) the MiniLM model *before* any test applies
    workload.determinism's RLIMIT_AS ceiling.

    In production the model is vendored into the image at build time
    (docs/project/PROJECT.md §5.2) — the running container never downloads it, so the
    memory ceiling only ever has to bound *inference*, not a network
    fetch plus a Rust download-accelerator's own buffers. Test envs don't
    have that vendoring step, so any test that both calls
    determinism.apply() and touches stage_features must depend on this
    fixture first, or it's measuring a memory profile production never has.
    """
    from cairn.workload import stage_features

    stage_features._get_model(stage_features.MAX_SEQ_LENGTH)
