from __future__ import annotations

import json
import subprocess
import sys
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


def test_commands_load_project_dotenv_without_truncating_ampersands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = (
        "postgresql://cairn:secret@example.invalid/defaultdb?sslmode=verify-full"
        "&options=--cluster%3Dcairn"
    )
    (tmp_path / ".env").write_text(f"CAIRN_DATABASE_URL={database_url}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAIRN_DATABASE_URL", raising=False)

    result = CliRunner().invoke(app, ["init", "--config", str(tmp_path / "cairn.yaml")])

    assert result.exit_code == 0, result.output
    assert cli_module.os.environ["CAIRN_DATABASE_URL"] == database_url


def test_project_dotenv_never_overrides_an_explicit_process_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("CAIRN_DATABASE_URL=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAIRN_DATABASE_URL", "from-process")

    result = CliRunner().invoke(app, ["init", "--config", str(tmp_path / "cairn.yaml")])

    assert result.exit_code == 0, result.output
    assert cli_module.os.environ["CAIRN_DATABASE_URL"] == "from-process"


def test_doctor_aws_falls_back_to_the_real_cli_when_the_sdk_probe_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == sys.executable:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="OPENSSL_Uplink: no OPENSSL_Applink",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123:user/x"}),
            stderr="",
        )

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli_module.shutil, "which", lambda command: "aws.exe" if command == "aws" else None
    )

    ok, detail = cli_module._doctor_aws()

    assert ok is True
    assert detail == "credentials valid, account=123456789012 (AWS CLI fallback)"
    assert len(calls) == 2


def test_resolve_tui_entry_honors_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = tmp_path / "cairn-tui"
    entry.write_text("# fake native tui binary\n", encoding="utf-8")
    monkeypatch.setenv("CAIRN_TUI_ENTRY", str(entry))
    assert cli_module._resolve_tui_entry() == entry


def test_resolve_tui_entry_rejects_a_nonexistent_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_TUI_ENTRY", str(tmp_path / "does-not-exist"))
    assert cli_module._resolve_tui_entry() is None


def test_resolve_tui_entry_finds_this_repos_rust_build_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The interactive terminal is the native `tui-rs` binary now — the
    # bare invocation no longer depends on Node at all.
    monkeypatch.delenv("CAIRN_TUI_ENTRY", raising=False)
    suffix = ".exe" if sys.platform == "win32" else ""
    target = ROOT / "tui-rs" / "target"
    release = target / "release" / f"cairn-tui{suffix}"
    debug = target / "debug" / f"cairn-tui{suffix}"
    resolved = cli_module._resolve_tui_entry()
    if release.is_file():
        assert resolved == release
    elif debug.is_file():
        assert resolved == debug, "a debug build is the last-resort fallback"
    else:
        assert resolved is None


def test_launch_tui_fails_loudly_when_the_binary_is_not_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # main() only calls _launch_tui() when stdout is a real TTY, which
    # CliRunner never provides — so exercise the guard function directly.
    monkeypatch.setattr(cli_module, "_resolve_tui_entry", lambda: None)
    with pytest.raises(typer.Exit) as excinfo:
        cli_module._launch_tui()
    assert excinfo.value.exit_code == 1


def test_launch_tui_spawns_the_native_binary_and_pins_the_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = tmp_path / "cairn-tui"
    entry.write_text("# fake native tui binary\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "_resolve_tui_entry", lambda: entry)
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], env: dict[str, str], check: bool):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["env"] = env
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    with pytest.raises(typer.Exit) as excinfo:
        cli_module._launch_tui()

    assert excinfo.value.exit_code == 0
    # The binary is spawned directly — no `node` in front of it.
    assert captured["argv"] == [str(entry)]
    # The TUI re-invokes `<CAIRN_PYTHON> -m cairn.cli ...` for real work, so
    # it must inherit this exact interpreter rather than resolving one off
    # PATH and silently using a different venv.
    assert captured["env"]["CAIRN_PYTHON"] == sys.executable  # type: ignore[index]
