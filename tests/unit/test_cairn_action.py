"""argv_json / oci_image contracts for the composite Cairn Action (§16)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ACTION_DIR = Path(__file__).resolve().parents[2] / ".github" / "actions" / "cairn"


def test_action_yml_exists_and_names_argv_json() -> None:
    text = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
    assert "argv_json" in text
    assert "oci_image" in text
    assert "id-token" not in text  # permissions belong on the caller workflow


@pytest.mark.parametrize(
    "raw,ok",
    [
        ('["python","/workspace/examples/embed_mapper.py"]', True),
        ("python /workspace/examples/embed_mapper.py", False),
        ("[]", False),
        ('["python", 1]', False),
    ],
)
def test_argv_json_must_be_string_array(raw: str, ok: bool) -> None:
    """Mirror run.sh's parser: array of strings only — never a shell string."""
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError:
        assert not ok
        return
    valid = isinstance(argv, list) and bool(argv) and all(isinstance(x, str) for x in argv)
    assert valid is ok


def test_run_sh_rejects_non_digest_image(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip(
            "run.sh exercised under Linux/CI; Windows path/bash bridging is not the contract"
        )
    env = {
        **os.environ,
        "CAIRN_CONTRACT": "jsonl-map/v1",
        "CAIRN_ARGV_JSON": '["python","-c","pass"]',
        "CAIRN_INPUT_FILE": "in.jsonl",
        "CAIRN_ID_FIELD": "id",
        "CAIRN_PARTITIONS": "64",
        "CAIRN_OUTPUT_FILE": str(tmp_path / "out.jsonl"),
        "CAIRN_OCI_IMAGE": "357199110611.dkr.ecr.us-east-1.amazonaws.com/cairn:latest",
        "CAIRN_NAMESPACE": "ci",
        "CAIRN_RECEIPT_BASE_URL": "",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    (tmp_path / "out").write_text("", encoding="utf-8")
    (tmp_path / "summary").write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(ACTION_DIR / "run.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    combined = (proc.stderr + proc.stdout).lower()
    assert "sha256" in combined or "immutable" in combined


def test_oci_image_digest_ref_contract() -> None:
    """Same gate run.sh enforces — mutable tags are refused."""
    assert "@sha256:" in "357199110611.dkr.ecr.us-east-1.amazonaws.com/cairn@sha256:deadbeef"
    assert "@sha256:" not in "357199110611.dkr.ecr.us-east-1.amazonaws.com/cairn:latest"
