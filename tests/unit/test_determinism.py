"""Unit test for the one part of determinism.py that's checkable without
actually running a model: the PYTHONHASHSEED guard. See the module
docstring for why this can only be checked, never fixed, from inside a
running process.
"""

from __future__ import annotations

import os

import pytest

from cairn.workload import determinism


def test_raises_if_hash_seed_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    with pytest.raises(RuntimeError, match="PYTHONHASHSEED"):
        determinism._check_hash_seed()


def test_raises_if_hash_seed_set_to_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONHASHSEED", "1")
    with pytest.raises(RuntimeError, match="PYTHONHASHSEED"):
        determinism._check_hash_seed()


def test_passes_when_hash_seed_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    determinism._check_hash_seed()  # must not raise


def test_current_process_actually_has_it_pinned() -> None:
    # Makefile exports PYTHONHASHSEED=0 for every recipe (see Makefile) —
    # this catches the case where a test runner bypasses the Makefile and
    # runs pytest directly without it set, which would otherwise pass all
    # the other tests here while silently proving the wrong thing.
    assert os.environ.get("PYTHONHASHSEED") == "0", (
        "run tests via `make test` (or export PYTHONHASHSEED=0 yourself) — "
        "workload determinism depends on it and this suite should not lie "
        "about that by passing anyway"
    )
