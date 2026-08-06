from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from cairn.fingerprint.workkey import config_fingerprint, make_work_key


def _key(*, config: dict[str, object], upstream: list[str]) -> str:
    return make_work_key(
        stage="eval",
        code_fingerprint="code",
        data_fingerprint="data",
        config_fingerprint=config_fingerprint(config),
        env_fingerprint="env",
        upstream=upstream,
    ).value


@settings(max_examples=500)
@given(
    values=st.dictionaries(
        st.text(min_size=1, max_size=12), st.integers(min_value=-1000, max_value=1000), max_size=8
    )
)
def test_work_key_is_invariant_under_config_mapping_order(values: dict[str, int]) -> None:
    reversed_values = dict(reversed(list(values.items())))
    assert _key(config=values, upstream=["b", "a"]) == _key(
        config=reversed_values, upstream=["a", "b"]
    )


@settings(max_examples=500)
@given(value=st.integers(), changed=st.integers())
def test_work_key_changes_when_a_causal_config_value_changes(value: int, changed: int) -> None:
    if value != changed:
        assert _key(config={"eval.value": value}, upstream=[]) != _key(
            config={"eval.value": changed}, upstream=[]
        )


def test_empty_stage_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="stage"):
        make_work_key(
            stage=" ",
            code_fingerprint="code",
            data_fingerprint="data",
            config_fingerprint="config",
            env_fingerprint="env",
        )
