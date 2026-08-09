#!/usr/bin/env python3
"""Project-controlled mapper used by Flight Recorder demos.

Two modes:

``--selftest`` — Day-2 tiny deterministic workspace touch so local-in-image
and Fargate traces can compare the same semantic resource set.

``--leaf`` — the Day-4 cooperative per-leaf mapper protocol (PLAN §18). Reads
one immutable canonical bucket slice, computes one real MiniLM embedding per
record (one at a time — batching must never be able to change a floating
point result), and emits a length-prefixed canonical-JSON checkpoint frame
to ``CAIRN_CHECKPOINT_FD`` after every fixed 8-record microchunk. The
supervisor (``cairn.flight.leaf_pipeline``), not this process, decides
whether a frame is durable — this process only proposes evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from cairn.adapters import jsonl_map

MAX_FRAME_BYTES = 64 * 1024
EMBEDDING_DIM = 384


def selftest(workspace: Path) -> int:
    """Read a baked fixture, probe a missing path, write one output file."""
    data_dir = workspace / "examples" / "_selftest"
    input_path = data_dir / "input.txt"
    if not input_path.is_file():
        print(f"selftest: missing fixture {input_path}", file=sys.stderr)
        return 2
    missing = data_dir / "does-not-exist.txt"
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


def _load_resume_manifest(path: str | None) -> dict[int, str]:
    """chunk_index -> already-committed input_digest. Empty if unset/absent."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {int(row["microchunk_index"]): str(row["input_digest"]) for row in data.get("committed", [])}


def _encode_one(model: Any, text: str) -> list[float]:
    """One record at a time — the §18 conformance requirement that grouping
    cannot alter floating point output; batching earlier stages is fine
    (stage_features.py), but this per-leaf mapper must not."""
    import numpy as np

    vec = model.encode([text], batch_size=1, show_progress_bar=False, convert_to_numpy=True)
    arr = np.asarray(vec, dtype="<f4")[0]
    return [float(x) for x in arr.tolist()]


def _get_model() -> Any:
    from cairn.workload.stage_features import MAX_SEQ_LENGTH
    from cairn.workload.stage_features import _get_model as _shared_model

    return _shared_model(MAX_SEQ_LENGTH)


def _text_for_row(obj: dict[str, Any]) -> str:
    """Deterministic text projection of a row for embedding — prefers an
    explicit `text` field (CairnBench rows carry one); falls back to the
    row's own canonical JSON so every input shape still produces a stable
    embedding rather than failing the leaf."""
    text = obj.get("text")
    if isinstance(text, str) and text:
        return text
    from cairn.fingerprint.canon import canonical_json

    return canonical_json(obj).decode("utf-8")


def run_leaf(
    *,
    bucket: int,
    input_slice: Path,
    id_field: str,
    checkpoint_fd: int,
    checkpoint_dir: Path,
    resume_manifest: str | None,
) -> int:
    rows: list[jsonl_map.Row] = []
    with input_slice.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            stable_id = obj[id_field]
            id_bytes = jsonl_map.canonical_id_bytes(stable_id)
            from cairn.fingerprint.canon import canonical_json

            rows.append(
                jsonl_map.Row(stable_id=stable_id, id_bytes=id_bytes, line_bytes=canonical_json(obj))
            )
    rows.sort(key=lambda r: r.id_bytes)
    raw_by_id = {r.id_bytes: json.loads(r.line_bytes) for r in rows}

    already_committed = _load_resume_manifest(resume_manifest)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    model = _get_model()
    checkpoint_out = os.fdopen(checkpoint_fd, "wb", buffering=0, closefd=True)
    try:
        for chunk in jsonl_map.microchunks(bucket, rows):
            skip_digest = already_committed.get(chunk.index)
            if skip_digest == chunk.input_digest:
                continue  # already durable; supervisor has the committed blob

            out_lines: list[bytes] = []
            ordered_ids: list[Any] = []
            for row in chunk.rows:
                obj = raw_by_id[row.id_bytes]
                text = _text_for_row(obj)
                embedding = _encode_one(model, text)
                out_obj = {id_field: obj[id_field], "embedding": embedding}
                from cairn.fingerprint.canon import canonical_json as _cj

                out_lines.append(_cj(out_obj))
                ordered_ids.append(obj[id_field])

            body = b"\n".join(out_lines) + b"\n"

            fd, tmp_name = tempfile.mkstemp(dir=str(checkpoint_dir), prefix=".chunk-")
            try:
                with os.fdopen(fd, "wb") as tmp_handle:
                    tmp_handle.write(body)
                    tmp_handle.flush()
                    os.fsync(tmp_handle.fileno())
                final_name = f"chunk-{chunk.bucket:02d}-{chunk.index:06d}.jsonl"
                final_path = checkpoint_dir / final_name
                os.replace(tmp_name, final_path)
            except Exception:
                Path(tmp_name).unlink(missing_ok=True)
                raise

            import hashlib

            claimed_output_digest = hashlib.sha256(body).hexdigest()
            frame = {
                "protocol": "cairn-checkpoint/v1",
                "chunk_index": chunk.index,
                "input_digest": chunk.input_digest,
                "ordered_ids": ordered_ids,
                "temp_relpath": final_name,
                "claimed_output_digest": claimed_output_digest,
            }
            from cairn.fingerprint.canon import canonical_json as _cj2

            frame_bytes = _cj2(frame)
            if len(frame_bytes) > MAX_FRAME_BYTES:
                print(f"leaf: checkpoint frame exceeds {MAX_FRAME_BYTES} bytes", file=sys.stderr)
                return 3
            checkpoint_out.write(len(frame_bytes).to_bytes(8, "big"))
            checkpoint_out.write(frame_bytes)
            checkpoint_out.flush()
    finally:
        checkpoint_out.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cairn embed mapper")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--leaf", action="store_true", help="Run the §18 cooperative leaf protocol.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--id-field", default="id")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest(args.workspace.resolve())

    if args.leaf:
        input_slice = os.environ.get("CAIRN_INPUT_SLICE")
        checkpoint_fd = os.environ.get("CAIRN_CHECKPOINT_FD")
        checkpoint_dir = os.environ.get("CAIRN_CHECKPOINT_DIR")
        bucket_env = os.environ.get("CAIRN_BUCKET")
        if not input_slice or not checkpoint_fd or not checkpoint_dir or bucket_env is None:
            print(
                "leaf: CAIRN_INPUT_SLICE, CAIRN_CHECKPOINT_FD, CAIRN_CHECKPOINT_DIR, "
                "CAIRN_BUCKET are required",
                file=sys.stderr,
            )
            return 2
        return run_leaf(
            bucket=int(bucket_env),
            input_slice=Path(input_slice),
            id_field=args.id_field,
            checkpoint_fd=int(checkpoint_fd),
            checkpoint_dir=Path(checkpoint_dir),
            resume_manifest=os.environ.get("CAIRN_RESUME_MANIFEST"),
        )

    parser.error("no action specified; try --selftest or --leaf")
    return 2


if __name__ == "__main__":
    sys.exit(main())
