"""The one connection pool every Cairn process shares.

Sizing follows the designing-application-transactions skill's HikariCP
guidance (.agents/skills/designing-application-transactions/SKILL.md §13):
small and fixed beats large and bursty on a serializable, contention-
sensitive workload — a bigger pool just means more transactions racing
for the same hot rows in db/claims.py.
"""

from __future__ import annotations

import os

from psycopg_pool import ConnectionPool

_POOL: ConnectionPool | None = None


def _env_url() -> str:
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "CAIRN_DATABASE_URL is not set — see .env.example and scripts/provision_cluster.sh."
        )
    return url


def get_pool(database_url: str | None = None, *, app_name: str = "cairn") -> ConnectionPool:
    """Return the process-wide pool, opening it lazily on first use.

    A single pool per process, not per call site, is the point: CockroachDB
    connections are relatively expensive to establish (TLS handshake to a
    cloud cluster), and every db/*.py module in Cairn should share the same
    bounded set rather than each opening its own.
    """
    global _POOL
    if _POOL is None:
        conninfo = database_url or _env_url()
        _POOL = ConnectionPool(
            conninfo=conninfo,
            min_size=2,
            max_size=10,
            max_lifetime=1800,  # 30 min, matches the skill's HikariCP maxLifetime guidance
            max_idle=600,
            # Default is 30s — on a real outage (DNS failure, host
            # unreachable) that means every checkout silently burns 30s
            # before PoolTimeout even reaches db/txn.py's retry loop. 10s
            # is still generous for a real CockroachDB Cloud TLS handshake
            # under normal conditions, and makes a genuine failure surface
            # in seconds instead of tens of seconds to a CLI user.
            timeout=10,
            kwargs={"application_name": app_name},
            open=True,
        )
    return _POOL


def close_pool() -> None:
    """Close and forget the process-wide pool. Tests call this between cases."""
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None
