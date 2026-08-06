"""Unit tests for cairn.db.txn.in_txn — no live database.

The one behaviour this module exists to guarantee is whole-closure replay
on SQLSTATE 40001 (see cairn/db/txn.py's module docstring): a retry must
re-run fn from scratch, not resume it, and it must not leak into infinite
retries or eat a real exception it shouldn't retry.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import SerializationFailure

from cairn.db import txn


class _FakeCursor:
    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeConnection:
    """Stands in for a pooled psycopg.Connection.

    Real rollback-on-exception behaviour lives in psycopg_pool and is
    exercised against a live cluster in tests/integration/test_claims.py.
    This fake only needs to support the control flow in_txn drives:
    open a cursor, run fn, commit — and propagate whatever fn raises.
    """

    def __init__(self) -> None:
        self.isolation_level: Any = None
        self.committed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.connections_opened = 0

    def connection(self) -> _FakeConnection:
        self.connections_opened += 1
        return _FakeConnection()


def _flaky_fn(fail_times: int, calls: list[int]):
    """Returns a closure that fails with SerializationFailure fail_times
    times, then succeeds. `calls` records one entry per invocation so the
    test can assert the closure was replayed whole, not resumed."""

    def fn(cur: object) -> str:
        calls.append(1)
        if len(calls) <= fail_times:
            raise SerializationFailure("restart transaction: retry txn")
        return "ok"

    return fn


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the backoff sleep with a recorder so retry tests run instantly."""
    slept: list[float] = []
    monkeypatch.setattr(txn.time, "sleep", lambda s: slept.append(s))
    return slept


def test_succeeds_without_retry_on_first_attempt() -> None:
    pool = _FakePool()
    calls: list[int] = []
    fn = _flaky_fn(fail_times=0, calls=calls)

    result = txn.in_txn(pool, fn, op="test.no_conflict")

    assert result == "ok"
    assert len(calls) == 1
    assert pool.connections_opened == 1


def test_replays_whole_closure_on_serialization_failure(
    _no_real_sleep: list[float],
) -> None:
    pool = _FakePool()
    calls: list[int] = []
    fn = _flaky_fn(fail_times=3, calls=calls)

    result = txn.in_txn(pool, fn, op="test.contended")

    # fn ran 4 times total: 3 failures + 1 success. Each failed call opened
    # its own connection, proving the *whole* transaction was replayed, not
    # just the failing statement.
    assert result == "ok"
    assert len(calls) == 4
    assert pool.connections_opened == 4
    assert len(_no_real_sleep) == 3  # one backoff between each retry


def test_backoff_is_bounded_and_increasing(_no_real_sleep: list[float]) -> None:
    pool = _FakePool()
    calls: list[int] = []
    fn = _flaky_fn(fail_times=5, calls=calls)

    txn.in_txn(pool, fn, op="test.backoff_shape")

    # jitter is +/-25%, so consecutive backoffs aren't strictly monotone at
    # the boundary, but the ceiling must hold and the trend must be upward.
    assert all(s <= (txn.CAP_MS * 1.25) / 1000.0 for s in _no_real_sleep)
    assert _no_real_sleep[-1] > _no_real_sleep[0]


def test_exhausts_retries_and_raises(_no_real_sleep: list[float]) -> None:
    pool = _FakePool()
    calls: list[int] = []
    fn = _flaky_fn(fail_times=txn.MAX_ATTEMPTS, calls=calls)  # never succeeds

    with pytest.raises(txn.TxnRetriesExhausted):
        txn.in_txn(pool, fn, op="test.always_contended")

    assert len(calls) == txn.MAX_ATTEMPTS
    assert len(_no_real_sleep) == txn.MAX_ATTEMPTS  # sleeps after every failed attempt


def test_non_serialization_error_propagates_immediately(
    _no_real_sleep: list[float],
) -> None:
    pool = _FakePool()

    def fn(cur: object) -> None:
        raise ValueError("not a retryable error")

    with pytest.raises(ValueError):
        txn.in_txn(pool, fn, op="test.real_bug")

    assert pool.connections_opened == 1  # never retried
    assert _no_real_sleep == []
