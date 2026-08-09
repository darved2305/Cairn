#!/usr/bin/env python3
"""Project-controlled mapper used by Flight Recorder demos.

Day-2 ``--selftest`` exercises a tiny deterministic workspace touch so
local-in-image and Fargate traces can compare the same semantic resource set.
The full jsonl-map adapter lands on Day 4.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def selftest(workspace: Path) -> int:
    """Read a baked fixture, probe a missing path, write one output file."""
    data_dir = workspace / "examples" / "_selftest"
    input_path = data_dir / "input.txt"
    if not input_path.is_file():
        print(f"selftest: missing fixture {input_path}", file=sys.stderr)
        return 2
    missing = data_dir / "does-not-exist.txt"
    # Negative lookup — must appear as NEGATIVE in a complete Linux trace.
    missing_exists = missing.exists()
    payload = {
        "marker": "cairn-selftest-v1",
        "input_bytes": input_path.read_bytes().hex(),
        "missing_exists": missing_exists,
    }
    out_dir = workspace / ".cairn" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "selftest.json"
    out_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(out_path)}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cairn embed mapper")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Run the Day-2 Flight Recorder self-test contract.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root (default: cwd).",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest(args.workspace.resolve())
    parser.error("no action specified; try --selftest (full mapper arrives Day 4)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
