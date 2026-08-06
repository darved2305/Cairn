"""Unit + property tests for cairn.fingerprint.canon.

canonical_json is the primitive every fingerprint in Cairn is built on
(work_key, env fingerprint) — PLAN.md §5 lists "canonical JSON stable
under key permutation" explicitly as a required property test.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairn.fingerprint.canon import canonical_float32_bytes, canonical_json

_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(max_size=20),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.dictionaries(st.text(max_size=10), children, max_size=5),
    max_leaves=10,
)


def test_key_order_does_not_affect_output() -> None:
    a = {"z": 1, "a": 2, "m": 3}
    b = {"a": 2, "m": 3, "z": 1}
    assert canonical_json(a) == canonical_json(b)


def test_different_values_produce_different_output() -> None:
    assert canonical_json({"a": 1}) != canonical_json({"a": 2})


def test_no_incidental_whitespace() -> None:
    assert canonical_json({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


@given(obj=st.dictionaries(st.text(min_size=1, max_size=10), _json_values, max_size=8))
def test_canonical_json_stable_under_key_permutation(obj: dict) -> None:
    # Rebuild the same dict via a different insertion order — canonical_json
    # must not care, since Python dicts remember insertion order but the
    # fingerprint must not depend on it.
    shuffled = dict(reversed(list(obj.items())))
    assert canonical_json(obj) == canonical_json(shuffled)


@given(obj=_json_values)
def test_canonical_json_deterministic_across_calls(obj: object) -> None:
    assert canonical_json(obj) == canonical_json(obj)


def test_float32_bytes_same_values_different_dtype_input() -> None:
    a = np.array([1.0, 2.5, -3.0], dtype=np.float64)
    b = np.array([1.0, 2.5, -3.0], dtype=np.float32)
    assert canonical_float32_bytes(a) == canonical_float32_bytes(b)


def test_float32_bytes_forces_little_endian() -> None:
    native = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    big_endian = native.astype(">f4")
    assert canonical_float32_bytes(native) == canonical_float32_bytes(big_endian)


def test_float32_bytes_non_contiguous_view_matches_contiguous_copy() -> None:
    base = np.arange(20, dtype=np.float32).reshape(4, 5)
    view = base[:, ::2]  # non-contiguous slice
    assert canonical_float32_bytes(view) == canonical_float32_bytes(np.ascontiguousarray(view))


def test_float32_bytes_length_matches_element_count() -> None:
    arr = np.zeros(10, dtype=np.float32)
    assert len(canonical_float32_bytes(arr)) == 10 * 4


@pytest.mark.parametrize("shape", [(0,), (1,), (5,), (3, 4)])
def test_float32_bytes_handles_various_shapes(shape: tuple[int, ...]) -> None:
    arr = np.random.default_rng(0).random(shape).astype(np.float32)
    assert canonical_float32_bytes(arr) == canonical_float32_bytes(arr.copy())
