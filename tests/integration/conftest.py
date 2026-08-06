"""Shared fixtures for tests that need a real CockroachDB cluster.

Nothing in tests/integration/ mocks the database — see PLAN.md §5's
anti-simulation rule. If there's no live cluster to test against, these
tests skip cleanly instead of faking a result.
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
