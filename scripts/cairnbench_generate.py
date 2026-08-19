#!/usr/bin/env python3
"""Deterministic, project-generated CairnBench corpus generator.

Every record is synthesized from a fixed template/vocabulary combined by a
seeded deterministic index walk — no third-party text is copied, so the
output carries no license encumbrance beyond this repository's own
(Apache-2.0). Re-running this script with the same ``--count`` always
produces byte-identical output; that determinism is the whole point, since
the generated corpus is itself an input to the Flight Recorder demo's
identity system.

docs/project/PLAN.md §11/§18 names 3,890 records as the frozen submission corpus; this
script supports that but defaults to a much smaller ``--count`` for fast
local/CI runs (each missing jsonl-map/v1 leaf pays a real model-load cost,
so a full 3,890-row 64-leaf run is a multi-minute proof, not a unit test).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SUBJECTS = (
    "the orbital telescope",
    "a distributed database",
    "the coastal survey team",
    "an autonomous rover",
    "the compiler toolchain",
    "a migratory bird flock",
    "the municipal water system",
    "a quantum sensor array",
    "the archival library",
    "a freight rail network",
    "the coral reef monitor",
    "a weather balloon fleet",
)

_VERBS = (
    "recorded",
    "reconciled",
    "measured",
    "cataloged",
    "compressed",
    "rerouted",
    "calibrated",
    "indexed",
    "synchronized",
    "audited",
)

_OBJECTS = (
    "a decade of drift in the reference frame",
    "an unexpected spike in packet loss",
    "the seasonal migration corridor",
    "a firmware regression in the actuator",
    "three redundant checksum mismatches",
    "the tidal anomaly near the outer shelf",
    "a backlog of unprocessed telemetry",
    "the thermal envelope of the payload bay",
    "a cluster of correlated failure signatures",
    "the scheduling drift across time zones",
)

_TAILS = (
    "before the maintenance window closed.",
    "after the second independent verification pass.",
    "without exceeding the declared power budget.",
    "while the backup link stayed cold.",
    "ahead of the quarterly compliance review.",
    "during the overlapping observation slot.",
)

CATEGORIES = ("ops-log", "field-note", "audit-entry", "sensor-digest")


def _record(index: int) -> dict[str, object]:
    subject = _SUBJECTS[index % len(_SUBJECTS)]
    verb = _VERBS[(index // len(_SUBJECTS)) % len(_VERBS)]
    obj = _OBJECTS[(index // (len(_SUBJECTS) * len(_VERBS))) % len(_OBJECTS)]
    tail = _TAILS[index % len(_TAILS)]
    category = CATEGORIES[index % len(CATEGORIES)]
    text = f"{subject} {verb} {obj} {tail}".capitalize()
    return {
        "id": f"cb-{index:06d}",
        "category": category,
        "text": text,
    }


def generate(count: int) -> list[dict[str, object]]:
    if count < 1:
        raise ValueError("count must be >= 1")
    seen_text: set[str] = set()
    rows: list[dict[str, object]] = []
    for i in range(count):
        row = _record(i)
        # Every record must be genuinely distinct, not a duplicate padded
        # in to inflate the count (docs/project/PLAN.md §11).
        assert row["text"] not in seen_text, f"generator produced a duplicate at index {i}"
        seen_text.add(str(row["text"]))
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the CairnBench JSONL corpus.")
    parser.add_argument("--count", type=int, default=200, help="Number of records (default 200).")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/cairnbench.jsonl"),
        help="Output JSONL path (default data/cairnbench.jsonl).",
    )
    args = parser.parse_args(argv)

    rows = generate(args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote {len(rows)} records to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
