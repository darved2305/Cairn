from __future__ import annotations

from pathlib import Path

from cairn.fingerprint.astcanon import canon_digest, extract_units


def _write(path: Path, source: str) -> str:
    path.write_text(source, encoding="utf-8")
    return canon_digest(path)


def test_comment_format_and_docstring_edits_are_invariant(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    first = _write(
        path,
        '''"""old docs"""\n\ndef add(a, b):\n    """old function docs"""\n    return a + b  # comment\n''',
    )
    second = _write(
        path,
        '''"""new docs"""\n\n# another comment\ndef add( a,b ):\n    """new function docs"""\n    return (a+b)\n''',
    )
    assert first == second


def test_semantic_edit_changes_digest(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    before = _write(path, "def value(x):\n    return x + 1\n")
    after = _write(path, "def value(x):\n    return x + 2\n")
    assert before != after


def test_top_level_constants_are_individual_code_units(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("LIMIT = 10\n\ndef run():\n    return LIMIT\n", encoding="utf-8")
    units, _ = extract_units(path, "sample")
    assert {unit.unit_id for unit in units} == {
        "sample:<module>",
        "sample:LIMIT",
        "sample:run",
    }
