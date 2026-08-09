#!/usr/bin/env python3
"""Independent no-cache reference for Gate C — PLAN.md §18's "clean
comparator": full recomputation of the exact frozen jsonl-map/v1
adapter/mapper algebra with lookup disabled (no Cairn claim/generation/CAS
machinery at all), so its output digest is a ground truth to compare
Cairn's leaf-repaired assembly against, not something Cairn itself produced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from cairn.adapters import jsonl_map
from cairn.fingerprint.canon import canonical_json


def _text_for_row(obj: dict[str, object]) -> str:
    text = obj.get("text")
    if isinstance(text, str) and text:
        return text
    return canonical_json(obj).decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--id-field", default="id")
    parser.add_argument("--output-file", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest = jsonl_map.validate_input(args.input_file, args.id_field)
    rows_by_bucket = manifest.buckets

    from cairn.workload.stage_features import MAX_SEQ_LENGTH, _get_model

    model = _get_model(MAX_SEQ_LENGTH)

    import numpy as np

    records: list[tuple[bytes, bytes]] = []
    for bucket_rows in rows_by_bucket:
        for row in bucket_rows:
            obj = json.loads(row.line_bytes)
            text = _text_for_row(obj)
            vec = model.encode([text], batch_size=1, show_progress_bar=False, convert_to_numpy=True)
            arr = np.asarray(vec, dtype="<f4")[0]
            out_obj = {args.id_field: obj[args.id_field], "embedding": [float(x) for x in arr.tolist()]}
            records.append((row.id_bytes, canonical_json(out_obj)))

    records.sort(key=lambda pair: pair[0])
    assembled = b"\n".join(line for _, line in records) + (b"\n" if records else b"")
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_bytes(assembled)
    digest = hashlib.sha256(assembled).hexdigest()
    print(json.dumps({"row_count": len(records), "clean_digest": digest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
