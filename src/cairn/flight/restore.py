"""Atomic single-file output restore (Day 3 whole-result memory).

Restore writes to a same-directory temp file, fsyncs, then ``os.replace``.
That is atomic for a regular-file target on the same filesystem — v0.1 does
not claim directory replace semantics.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path


def restore_output_atomic(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via temp + fsync + replace."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.cairn-restore-{uuid.uuid4().hex}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
