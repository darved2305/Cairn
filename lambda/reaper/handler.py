"""Lease reaper — PROJECT.md §4.5, D7.

Flips expired CLAIMED/RUNNING work_claims rows to ABANDONED. It never
deletes anything and never completes work itself: ABANDONED is already a
terminal state db/claims.py's acquire() recognizes (the "prior_terminal"
takeover path), so a fresh contender picks the row up on its own next
acquire() call. What the reaper actually buys is *bounded* takeover
latency — without it, an expired lease sits undiscovered until something
happens to retry that specific work_key, which could be arbitrarily long.

EventBridge's rate() expression cannot go below 1 minute (confirmed by a
real ValidationException — sub-minute units aren't accepted, not just
discouraged), so infra/events.tf triggers this once a minute and this
handler sweeps twice, REAPER_SWEEP_GAP_SECONDS apart (default 30), inside
that one invocation. The net effect in wall-clock time is still a genuine
sweep every 30s (t=0, 30, 60, 90, ...) — the halving happens inside the
Lambda, not by quietly accepting a 60s cadence.

Deliberately NOT built on the `cairn` package (db/txn.py, db/claims.py):
the ECS image (Dockerfile) is the one-image-one-deploy-path artifact for
the *workload* — torch, sentence-transformers, pandas — and dragging that
dependency tree into a Lambda whose entire job is one UPDATE statement
every 30 seconds would be pure waste. This file is intentionally
self-contained, with its own small serializable-retry loop mirroring
db/txn.py's (50ms base, x2, +/-25% jitter, cap 2s, 8 attempts) — same
policy, independent deployment unit.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any

import psycopg
from psycopg.errors import SerializationFailure

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_ATTEMPTS = 8
BASE_MS = 50
CAP_MS = 2000

_REAP_SQL = """
    UPDATE work_claims
       SET state = 'ABANDONED', updated_at = now()
     WHERE state IN ('CLAIMED', 'RUNNING')
       AND lease_expires_at < now()
    RETURNING work_key, stage, owner_id, owner_region, fence
"""


def _database_url() -> str:
    secret_arn = os.environ.get("CAIRN_DATABASE_URL_SECRET_ARN")
    if secret_arn:
        import boto3

        client = boto3.client("secretsmanager")
        secret: str = client.get_secret_value(SecretId=secret_arn)["SecretString"]
        return secret
    url = os.environ.get("CAIRN_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "neither CAIRN_DATABASE_URL_SECRET_ARN nor CAIRN_DATABASE_URL is set"
        )
    return url


def _reap_once(database_url: str) -> list[tuple[str, str, str, str, int]]:
    last: SerializationFailure | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with psycopg.connect(database_url) as conn:
                conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
                with conn.cursor() as cur:
                    cur.execute(_REAP_SQL)
                    rows: list[tuple[str, str, str, str, int]] = cur.fetchall()
                conn.commit()
            return rows
        except SerializationFailure as exc:
            last = exc
            backoff_ms = min(CAP_MS, BASE_MS * (2**attempt))
            time.sleep((backoff_ms * random.uniform(0.75, 1.25)) / 1000.0)
    raise RuntimeError(f"reaper sweep exhausted {MAX_ATTEMPTS} retries") from last


def _log_reaped(reaped: list[tuple[str, str, str, str, int]]) -> None:
    for work_key, stage, owner_id, owner_region, fence in reaped:
        logger.info(
            json.dumps(
                {
                    "metric": "ReaperReaped",
                    "work_key": work_key,
                    "stage": stage,
                    "owner_id": owner_id,
                    "owner_region": owner_region,
                    "fence": fence,
                }
            )
        )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    database_url = _database_url()
    gap_seconds = float(os.environ.get("REAPER_SWEEP_GAP_SECONDS", "30"))

    first = _reap_once(database_url)
    _log_reaped(first)

    time.sleep(gap_seconds)

    second = _reap_once(database_url)
    _log_reaped(second)

    return {"reaped_count": len(first) + len(second)}
