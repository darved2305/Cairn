from __future__ import annotations

import ast

from hypothesis import given, settings
from hypothesis import strategies as st

from cairn.fingerprint.astcanon import CodeUnit, node_digest
from cairn.fingerprint.workkey import code_fingerprint, make_work_key


def _work_key(source: str) -> str:
    unit = CodeUnit(
        unit_id="demo.stage:run",
        module="demo.stage",
        qualname="run",
        ast_digest=node_digest(ast.parse(source)),
        docstring_digest="not-causal",
        is_private=False,
        source_path="demo/stage.py",
    )
    return make_work_key(
        stage="demo",
        code_fingerprint=code_fingerprint([unit]),
        data_fingerprint="data",
        config_fingerprint="config",
        env_fingerprint="env",
    ).value


@settings(max_examples=500)
@given(value=st.integers(min_value=-10_000, max_value=10_000))
def test_work_key_ignores_comments_and_format_but_changes_for_semantics(value: int) -> None:
    compact = f"def run(x):\n return x + {value} # incidental comment\n"
    formatted = f'def run(x):\n    """changed docs"""\n    return (x + {value})\n'
    semantic_edit = f"def run(x):\n    return x + {value + 1}\n"

    assert _work_key(compact) == _work_key(formatted)
    assert _work_key(compact) != _work_key(semantic_edit)
