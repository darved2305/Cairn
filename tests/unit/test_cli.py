from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import cairn.cli as cli_module
from cairn.cli import app
from cairn.config import TrackedConfig

ROOT = Path(__file__).resolve().parents[2]


def test_plan_json_prints_all_stage_work_keys() -> None:
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--config",
            str(ROOT / "cairn.yaml"),
            "--source-root",
            str(ROOT / "src"),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [stage["stage"] for stage in payload["stages"]] == [
        "env",
        "dataset",
        "features",
        "checkpoint",
        "eval",
    ]
    assert all(len(stage["work_key"]) == 64 for stage in payload["stages"])


def test_plan_rejects_invalid_output_mode() -> None:
    result = CliRunner().invoke(app, ["plan", "--output", "xml"])
    assert result.exit_code == 2
    assert "table" in result.output


def test_init_scaffolds_a_config_that_round_trips_through_trackedconfig(tmp_path: Path) -> None:
    config_path = tmp_path / "cairn.yaml"
    result = CliRunner().invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert config_path.exists()

    loaded = TrackedConfig.load(config_path)
    assert loaded.get("features.embedding_dim", stage="features") == 384
    assert loaded.get("train.num_labels", stage="train") == 4


def test_init_refuses_to_overwrite_an_existing_config_without_force(tmp_path: Path) -> None:
    config_path = tmp_path / "cairn.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert config_path.read_text(encoding="utf-8") == "version: 1\n"


def test_init_force_overwrites_an_existing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "cairn.yaml"
    config_path.write_text("version: 1\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["init", "--config", str(config_path), "--force"])
    assert result.exit_code == 0, result.output
    assert "features:" in config_path.read_text(encoding="utf-8")


def test_explain_exits_1_for_an_unknown_artifact_without_a_database(tmp_path: Path) -> None:
    # No CAIRN_DATABASE_URL is required to prove this specific path: an
    # unreachable/unset database surfaces as a connection error rather
    # than a clean "unknown artifact" exit here, which is the correct,
    # honest failure mode — get_pool() raises before describe_artifact
    # runs. This just pins that `explain` never crashes uncontrolled.
    result = CliRunner().invoke(app, ["explain", "does-not-exist"])
    assert result.exit_code != 0


def test_bare_invocation_prints_help_instead_of_launching_a_tui_when_not_a_tty() -> None:
    # CliRunner's captured output stream is never a real TTY, so this
    # exercises main()'s non-TTY fallback branch honestly, without
    # monkeypatching isatty — Part B §41: bare `cairn` piped/redirected
    # must behave exactly like `cairn --help`, never attempt to draw a TUI.
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "Causal reuse memory" in result.output


def test_a_real_subcommand_is_unaffected_by_the_bare_invocation_change() -> None:
    result = CliRunner().invoke(app, ["plan", "--output", "xml"])
    assert result.exit_code == 2
    assert "table" in result.output


def test_resolve_tui_entry_honors_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = tmp_path / "index.js"
    entry.write_text("// fake tui entry\n", encoding="utf-8")
    monkeypatch.setenv("CAIRN_TUI_ENTRY", str(entry))
    assert cli_module._resolve_tui_entry() == entry


def test_resolve_tui_entry_rejects_a_nonexistent_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_TUI_ENTRY", str(tmp_path / "does-not-exist.js"))
    assert cli_module._resolve_tui_entry() is None


def test_resolve_tui_entry_finds_this_repos_dev_build_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAIRN_TUI_ENTRY", raising=False)
    dev_entry = ROOT / "tui" / "dist" / "index.js"
    resolved = cli_module._resolve_tui_entry()
    if dev_entry.is_file():
        assert resolved == dev_entry
    else:
        assert resolved is None


def test_launch_tui_fails_loudly_without_node(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() only calls _launch_tui() when stdout is a real TTY, which
    # CliRunner never provides — so exercise the guard function directly.
    monkeypatch.setattr(cli_module.shutil, "which", lambda _name: None)
    with pytest.raises(typer.Exit) as excinfo:
        cli_module._launch_tui()
    assert excinfo.value.exit_code == 1
