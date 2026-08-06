"""Shared fixtures for tests that need real external dependencies: a live
CockroachDB cluster, or the real MiniLM model.

Nothing in tests/integration/ mocks these — see PLAN.md §5's anti-simulation
rule. If a dependency isn't available, these tests skip cleanly instead of
faking a result.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from psycopg_pool import ConnectionPool

from cairn.db.pool import close_pool, get_pool


@pytest.fixture(scope="session")
def pool() -> Iterator[ConnectionPool]:
    if not os.environ.get("CAIRN_DATABASE_URL"):
        pytest.skip(
            "CAIRN_DATABASE_URL not set — integration tests need a live "
            "CockroachDB cluster (see scripts/provision_cluster.sh)"
        )
    p = get_pool()
    yield p
    close_pool()


@pytest.fixture(scope="session")
def warm_embedding_model() -> None:
    """Load (and HF-cache) the MiniLM model *before* any test applies
    workload.determinism's RLIMIT_AS ceiling.

    In production the model is vendored into the image at build time
    (PROJECT.md §5.2) — the running container never downloads it, so the
    memory ceiling only ever has to bound *inference*, not a network
    fetch plus a Rust download-accelerator's own buffers. Test envs don't
    have that vendoring step, so any test that both calls
    determinism.apply() and touches stage_features must depend on this
    fixture first, or it's measuring a memory profile production never has.
    """
    from cairn.workload import stage_features

    stage_features._get_model(stage_features.MAX_SEQ_LENGTH)
