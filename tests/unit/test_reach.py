from __future__ import annotations

from pathlib import Path

from cairn.fingerprint.reach import build_graph, reachable


def _package(tmp_path: Path, helper_source: str) -> Path:
    package = tmp_path / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "helper.py").write_text(helper_source, encoding="utf-8")
    (package / "stage.py").write_text(
        "from demo.helper import used\n\ndef run():\n    return used()\n\ndef unreachable():\n    return 99\n",
        encoding="utf-8",
    )
    return tmp_path


def test_reachable_follows_first_party_calls_but_not_unused_symbols(tmp_path: Path) -> None:
    root = _package(
        tmp_path, "LIMIT = 3\n\ndef used():\n    return LIMIT\n\ndef unused():\n    return 8\n"
    )
    result = reachable(build_graph(root), "demo.stage:run")
    assert "demo.helper:used" in result.units
    assert "demo.helper:LIMIT" in result.units
    assert "demo.helper:unused" not in result.units
    assert "demo.stage:unreachable" not in result.units
    assert result.sound


def test_dynamic_dispatch_in_reachable_code_is_hard_unsoundness(tmp_path: Path) -> None:
    root = _package(tmp_path, "def used():\n    return getattr(object(), 'value', None)\n")
    result = reachable(build_graph(root), "demo.stage:run")
    assert not result.sound
    assert "getattr" in result.escape_hatches


def test_escape_hatch_in_unreachable_symbol_does_not_poison_stage(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        "def used():\n    return 1\n\ndef unused():\n    return eval('2 + 2')\n",
    )
    result = reachable(build_graph(root), "demo.stage:run")
    assert result.sound
    assert not result.escape_hatches


def test_ordinary_method_named_eval_is_not_the_builtin_escape_hatch(tmp_path: Path) -> None:
    root = _package(
        tmp_path,
        "class Model:\n    def eval(self):\n        return self\n\ndef used():\n    return Model().eval()\n",
    )
    result = reachable(build_graph(root), "demo.stage:run")
    assert result.sound
    assert "eval" not in result.escape_hatches


def test_missing_entrypoint_is_rejected(tmp_path: Path) -> None:
    import pytest

    root = _package(tmp_path, "def used():\n    return 1\n")
    graph = build_graph(root)
    with pytest.raises(KeyError, match="entrypoint"):
        reachable(graph, "demo.stage:missing")
