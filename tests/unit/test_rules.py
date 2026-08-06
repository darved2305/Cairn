from __future__ import annotations

from pathlib import Path

from cairn.classify.rules import (
    ChangeClass,
    classify_comment_or_formatting,
    classify_downstream_only_config,
    classify_logging_only,
    classify_private_symbol_rename,
    classify_unreachable_change,
)
from cairn.fingerprint.astcanon import extract_units
from cairn.fingerprint.reach import build_graph


def _unit(path: Path, source: str, module: str, unit_id: str):
    path.write_text(source, encoding="utf-8")
    units, _ = extract_units(path, module)
    return next(u for u in units if u.unit_id == unit_id)


# ---------------------------------------------------------------------------
# comment_only / formatting_only
# ---------------------------------------------------------------------------


def test_docstring_only_edit_is_comment_only_and_allowed(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    old = _unit(path, '''def run(x):\n    """old docs"""\n    return x + 1\n''', "mod", "mod:run")
    new = _unit(
        path,
        '''def run(x):\n    """new docs, much longer"""\n    return x + 1\n''',
        "mod",
        "mod:run",
    )
    verdict = classify_comment_or_formatting(old, new, reachable_units=[new])
    assert verdict.change_class is ChangeClass.COMMENT_ONLY
    assert verdict.applies and not verdict.refused


def test_docstring_edit_refused_when_dunder_doc_is_read(tmp_path: Path) -> None:
    reader_path = tmp_path / "reader.py"
    reader_path.write_text(
        "def run():\n    from mod import target\n    return target.__doc__\n", encoding="utf-8"
    )
    target_path = tmp_path / "mod.py"
    old = _unit(
        target_path, '''def target():\n    """old"""\n    return 1\n''', "mod", "mod:target"
    )
    new = _unit(
        target_path, '''def target():\n    """new"""\n    return 1\n''', "mod", "mod:target"
    )
    reader_units, _ = extract_units(reader_path, "reader")
    reader_unit = next(u for u in reader_units if u.unit_id == "reader:run")

    verdict = classify_comment_or_formatting(old, new, reachable_units=[new, reader_unit])
    assert verdict.applies and verdict.refused
    assert "__doc__" in verdict.reason


def test_whitespace_only_edit_is_formatting_only_and_allowed(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    old = _unit(path, "def run(a, b):\n    return a + b  # note\n", "mod", "mod:run")
    new = _unit(path, "def run( a,  b ):\n    return (a+b)\n", "mod", "mod:run")
    verdict = classify_comment_or_formatting(old, new, reachable_units=[new])
    assert verdict.change_class is ChangeClass.FORMATTING_ONLY
    assert verdict.applies and not verdict.refused


def test_formatting_only_refused_when_py_file_read_as_text(tmp_path: Path) -> None:
    reader_path = tmp_path / "reader.py"
    reader_path.write_text("def run():\n    return open('config.py').read()\n", encoding="utf-8")
    target_path = tmp_path / "mod.py"
    old = _unit(target_path, "def run(a):\n    return a\n", "mod", "mod:run")
    new = _unit(target_path, "def run( a ):\n    return a\n", "mod", "mod:run")
    reader_units, _ = extract_units(reader_path, "reader")
    reader_unit = next(u for u in reader_units if u.unit_id == "reader:run")

    verdict = classify_comment_or_formatting(old, new, reachable_units=[new, reader_unit])
    assert verdict.applies and verdict.refused
    assert "open" in verdict.reason


def test_semantic_change_does_not_apply_to_comment_or_formatting(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    old = _unit(path, "def run(x):\n    return x + 1\n", "mod", "mod:run")
    new = _unit(path, "def run(x):\n    return x + 2\n", "mod", "mod:run")
    verdict = classify_comment_or_formatting(old, new, reachable_units=[new])
    assert not verdict.applies


# ---------------------------------------------------------------------------
# logging_only
# ---------------------------------------------------------------------------

_LOGGING_PREAMBLE = "import logging\n\nlogger = logging.getLogger(__name__)\n\n"


def _reachable_unit(tmp_path: Path, filename: str, source: str, module: str, unit_id: str):
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    units, _ = extract_units(path, module)
    return next(u for u in units if u.unit_id == unit_id)


def test_added_logger_debug_inside_loop_is_logging_only(tmp_path: Path) -> None:
    old_src = "def run(items):\n    total = 0\n    for x in items:\n        total += x\n    return total\n"
    new_src = (
        "def run(items):\n    total = 0\n    for x in items:\n"
        "        logger.debug('step %s', x)\n        total += x\n    return total\n"
    )
    new_file = _LOGGING_PREAMBLE + new_src
    new_unit = _reachable_unit(tmp_path, "mod.py", new_file, "mod", "mod:run")

    verdict = classify_logging_only(
        old_unit_source=old_src,
        new_unit_source=new_src,
        new_file_source=new_file,
        reachable_units=[new_unit],
    )
    assert verdict.change_class is ChangeClass.LOGGING_ONLY
    assert verdict.applies and not verdict.refused


def test_logging_only_does_not_apply_when_non_logging_code_also_changed(tmp_path: Path) -> None:
    old_src = "def run(x):\n    return x + 1\n"
    new_src = "def run(x):\n    logger.debug('x=%s', x)\n    return x + 2\n"
    new_file = _LOGGING_PREAMBLE + new_src
    new_unit = _reachable_unit(tmp_path, "mod.py", new_file, "mod", "mod:run")

    verdict = classify_logging_only(
        old_unit_source=old_src,
        new_unit_source=new_src,
        new_file_source=new_file,
        reachable_units=[new_unit],
    )
    assert not verdict.applies
    assert "not confined" in verdict.reason


def test_logging_only_refused_when_reachable_set_spawns_threads(tmp_path: Path) -> None:
    old_src = "def run(x):\n    return x\n"
    new_src = "def run(x):\n    logger.debug('x=%s', x)\n    return x\n"
    new_file = _LOGGING_PREAMBLE + new_src
    new_unit = _reachable_unit(tmp_path, "mod.py", new_file, "mod", "mod:run")

    spawner_unit = _reachable_unit(
        tmp_path,
        "spawner.py",
        "import threading\n\ndef spawn():\n    return threading.Thread(target=lambda: None)\n",
        "spawner",
        "spawner:spawn",
    )

    verdict = classify_logging_only(
        old_unit_source=old_src,
        new_unit_source=new_src,
        new_file_source=new_file,
        reachable_units=[new_unit, spawner_unit],
    )
    assert verdict.applies and verdict.refused
    assert "Thread" in verdict.reason


def test_logging_call_with_walrus_argument_does_not_apply(tmp_path: Path) -> None:
    old_src = "def run(x):\n    return x\n"
    new_src = "def run(x):\n    logger.debug('y=%s', (y := x + 1))\n    return x\n"
    new_file = _LOGGING_PREAMBLE + new_src
    new_unit = _reachable_unit(tmp_path, "mod.py", new_file, "mod", "mod:run")

    verdict = classify_logging_only(
        old_unit_source=old_src,
        new_unit_source=new_src,
        new_file_source=new_file,
        reachable_units=[new_unit],
    )
    assert not verdict.applies
    assert "not provably side-effect-free" in verdict.reason


# ---------------------------------------------------------------------------
# private_symbol_rename
# ---------------------------------------------------------------------------


def test_pure_private_rename_with_all_call_sites_updated(tmp_path: Path) -> None:
    old_def = "def _fmt_row(r):\n    return str(r)\n"
    new_def = "def _fmt_line(r):\n    return str(r)\n"
    old_run = "def run(rows):\n    return [_fmt_row(r) for r in rows]\n"
    new_run = "def run(rows):\n    return [_fmt_line(r) for r in rows]\n"
    path = tmp_path / "mod.py"
    path.write_text(new_def + "\n" + new_run, encoding="utf-8")
    units, _ = extract_units(path, "mod")
    new_fn_unit = next(u for u in units if u.unit_id == "mod:_fmt_line")
    new_run_unit = next(u for u in units if u.unit_id == "mod:run")

    verdict = classify_private_symbol_rename(
        "_fmt_row",
        "_fmt_line",
        changed_units={"def": (old_def, new_def), "run": (old_run, new_run)},
        reachable_units=[new_fn_unit, new_run_unit],
    )
    assert verdict.change_class is ChangeClass.PRIVATE_SYMBOL_RENAME
    assert verdict.applies and not verdict.refused


def test_private_symbol_rename_refused_on_dynamic_dispatch(tmp_path: Path) -> None:
    old_def = "def _helper():\n    return 1\n"
    new_def = "def _helper2():\n    return 1\n"
    new_fn_unit = _reachable_unit(tmp_path, "mod.py", new_def, "mod", "mod:_helper2")

    dispatcher_unit = _reachable_unit(
        tmp_path,
        "dispatch.py",
        "def run(obj, name):\n    return getattr(obj, name)\n",
        "dispatch",
        "dispatch:run",
    )

    verdict = classify_private_symbol_rename(
        "_helper",
        "_helper2",
        changed_units={"def": (old_def, new_def)},
        reachable_units=[new_fn_unit, dispatcher_unit],
    )
    assert verdict.applies and verdict.refused
    assert "getattr" in verdict.reason


def test_rename_does_not_apply_when_symbol_is_not_private() -> None:
    verdict = classify_private_symbol_rename(
        "helper", "helper2", changed_units={}, reachable_units=[]
    )
    assert not verdict.applies
    assert not verdict.refused


def test_rename_does_not_apply_when_body_also_changed_semantically(tmp_path: Path) -> None:
    old_def = "def _helper(x):\n    return x + 1\n"
    new_def = "def _helper2(x):\n    return x + 2\n"
    new_fn_unit = _reachable_unit(tmp_path, "mod.py", new_def, "mod", "mod:_helper2")

    verdict = classify_private_symbol_rename(
        "_helper",
        "_helper2",
        changed_units={"def": (old_def, new_def)},
        reachable_units=[new_fn_unit],
    )
    assert not verdict.applies
    assert "differs by more than the rename" in verdict.reason


# ---------------------------------------------------------------------------
# unreachable_change
# ---------------------------------------------------------------------------


def _pkg(tmp_path: Path, stage_body: str) -> Path:
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "stage.py").write_text(stage_body, encoding="utf-8")
    return tmp_path


def test_unreachable_change_applies_to_untouched_dead_code(tmp_path: Path) -> None:
    root = _pkg(
        tmp_path,
        "def run():\n    return 1\n\ndef dead():\n    return 2\n",
    )
    graph = build_graph(root)
    verdict = classify_unreachable_change(graph, graph, "demo.stage:run", {"demo.stage:dead"})
    assert verdict.change_class is ChangeClass.UNREACHABLE_CHANGE
    assert verdict.applies and not verdict.refused


def test_unreachable_change_does_not_apply_when_changed_unit_is_reachable(tmp_path: Path) -> None:
    root = _pkg(tmp_path, "def run():\n    return helper()\n\ndef helper():\n    return 2\n")
    graph = build_graph(root)
    verdict = classify_unreachable_change(graph, graph, "demo.stage:run", {"demo.stage:helper"})
    assert not verdict.applies


def test_unreachable_change_refused_on_unsound_graph(tmp_path: Path) -> None:
    root = _pkg(
        tmp_path,
        "def run():\n    return getattr(object(), 'x', None)\n\ndef dead():\n    return 1\n",
    )
    graph = build_graph(root)
    verdict = classify_unreachable_change(graph, graph, "demo.stage:run", {"demo.stage:dead"})
    assert verdict.applies and verdict.refused
    assert "getattr" in verdict.reason


# ---------------------------------------------------------------------------
# downstream_only_config
# ---------------------------------------------------------------------------


def test_downstream_only_config_applies_when_key_not_recorded() -> None:
    verdict = classify_downstream_only_config({"eval.metrics"}, {"train.num_labels"})
    assert verdict.change_class is ChangeClass.DOWNSTREAM_ONLY_CONFIG
    assert verdict.applies and not verdict.refused


def test_downstream_only_config_does_not_apply_when_key_was_read() -> None:
    verdict = classify_downstream_only_config({"train.hidden_dim"}, {"train.hidden_dim"})
    assert not verdict.applies


def test_downstream_only_config_does_not_apply_with_no_changes() -> None:
    verdict = classify_downstream_only_config(set(), {"train.hidden_dim"})
    assert not verdict.applies
