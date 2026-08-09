"""Collector and companion unit tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cairn.flight.types import (
    AccessMode,
    CoverageState,
    ObservationSource,
    ResourceIdentity,
    ResourceKind,
)
from cairn.trace.collector import collect, parse_strace_line, parse_strace_output
from cairn.trace.companion import COMPANION_VERSION, load_companion_resources, merge_companion
from cairn.trace.scout import ARBITRARY_EXEC_COVERAGE, run_scout


def test_parse_exit_line() -> None:
    event = parse_strace_line("42  +++ exited with 7 +++")
    assert event is not None
    assert event.kind == "exit"
    assert event.ret == "7"
    assert event.pid == 42


def test_parse_unparsed() -> None:
    event = parse_strace_line("garbage")
    assert event is not None
    assert event.kind == "incomplete"


def test_join_unfinished_resumed() -> None:
    text = """\
10  openat(AT_FDCWD, "/tmp/x", O_RDONLY <unfinished ...>
10  <... openat resumed>) = 3</tmp/x>
10  +++ exited with 0 +++
"""
    events, reasons = parse_strace_output(text)
    assert not any(e.kind == "incomplete" for e in events)
    assert any(e.name == "openat" for e in events)
    assert reasons == () or "truncated" not in "".join(reasons)


def test_gate_a_freeze() -> None:
    assert ARBITRARY_EXEC_COVERAGE is CoverageState.SHADOW_UNQUALIFIED


def test_companion_adds_only(tmp_path: Path) -> None:
    log = tmp_path / "c.jsonl"
    (tmp_path / "f.txt").write_text("hi", encoding="utf-8")
    log.write_text(
        json.dumps({"kind": "open", "path": str(tmp_path / "f.txt"), "mode": "r"})
        + "\n"
        + json.dumps({"kind": "env", "name": "CAIRN_MODE"})
        + "\n"
        + json.dumps({"kind": "import", "module": "embed_mapper"})
        + "\n",
        encoding="utf-8",
    )
    rows = load_companion_resources(log, workspace=tmp_path)
    assert any(r.source is ObservationSource.PYTHON for r in rows)
    assert any(r.kind is ResourceKind.ENV and r.ref == "CAIRN_MODE" for r in rows)
    assert COMPANION_VERSION == "python-audit/v1"

    base = (
        ResourceIdentity(
            kind=ResourceKind.FILE,
            ref="f.txt",
            access_mode=AccessMode.READ,
            exists=True,
            version_digest="ab" * 32,
            resolver="workspace_file/v1",
            source=ObservationSource.SYSCALL,
        ),
    )
    merged = merge_companion(base, rows)
    # Additive: base row preserved; companion may add env/module rows.
    assert any(r.source is ObservationSource.SYSCALL for r in merged)
    assert len(merged) >= len(base)


def test_scout_preserves_child_exit(tmp_path: Path) -> None:
    script = tmp_path / "exit.py"
    script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    result = run_scout(
        [sys.executable, str(script)],
        output_file=tmp_path / "out" / "x.bin",
        workspace=tmp_path,
    )
    assert result.child_exit_code == 3
    if sys.platform.startswith("linux"):
        assert result.trace.coverage_state in {
            CoverageState.SHADOW_UNQUALIFIED,
            CoverageState.INCOMPLETE_TRACE_LOSS,
            CoverageState.INCOMPLETE_NETWORK,
            CoverageState.INCOMPLETE_WRITE,
            CoverageState.INCOMPLETE_PLATFORM,
        }
    else:
        assert result.trace.coverage_state is CoverageState.INCOMPLETE_PLATFORM


def test_collect_non_linux_runs_command(tmp_path: Path) -> None:
    if sys.platform.startswith("linux"):
        return
    script = tmp_path / "ok.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    result = collect([sys.executable, str(script)], cwd=tmp_path)
    assert result.platform_supported is False
    assert result.child_exit_code == 0
    assert "not_linux" in result.incomplete_reasons
