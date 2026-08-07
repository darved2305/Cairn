"""Versioned event protocol for the Cairn TUI bridge (Part B §13 of the
TUI build spec).

Every event describes a real state transition that has already committed
— a claim acquired, a decision recorded, a contradiction raised, a stage
finished. Nothing here is ever called from inside a `db/txn.py::in_txn`
closure: `in_txn` retries its whole closure on SQLSTATE 40001, and a
closure that did I/O (including writing an event) would double-emit on
replay. Every call site in this codebase fires `emit_event` *after*
`in_txn` returns — the same rule `db/contradictions.py::record_contradiction`
already follows for `obs/metrics.emit_metric`, extended here to a typed
event instead of a CloudWatch metric line.

Silent by default. `emit_event` is a no-op unless `CAIRN_EVENTS_FILE`
names a writable path, so every existing `cairn` subcommand's stdout/stderr
is byte-identical whether or not this module is imported and called. The
TUI is the only thing that ever sets `CAIRN_EVENTS_FILE` — it creates a
temp file, spawns the Python subprocess with that path in its environment,
and tails the file for newline-delimited JSON envelopes
(tui/src/connection/subprocess.ts). A tailed plain file was chosen over
inherited file descriptors specifically because fd-inheritance across a
Node child_process boundary is POSIX-shaped and unreliable on Windows,
which this project develops and demos on; a regular file, opened in
append mode by the writer and polled by the reader, works identically on
every platform Cairn runs on.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TextIO

PROTOCOL_VERSION = 1

_lock = threading.Lock()
_stream: TextIO | None = None
_stream_path: str | None = None


def _resolve_stream() -> TextIO | None:
    """Lazily open (and cache) the events file named by CAIRN_EVENTS_FILE.
    Re-checks the env var on every call rather than caching "no path" once
    forever, because tests toggle CAIRN_EVENTS_FILE per-case via
    monkeypatch within one process."""

    global _stream, _stream_path
    path = os.environ.get("CAIRN_EVENTS_FILE")
    if not path:
        if _stream is not None:
            _stream.close()
            _stream = None
            _stream_path = None
        return None
    if _stream is not None and _stream_path == path:
        return _stream
    if _stream is not None:
        _stream.close()
    _stream = open(path, "a", encoding="utf-8")  # noqa: SIM115 - cached across calls
    _stream_path = path
    return _stream


def emit_event(
    event_type: str,
    payload: Mapping[str, object],
    *,
    run_id: str | None = None,
) -> None:
    """Write one NDJSON event envelope. No-op unless CAIRN_EVENTS_FILE is
    set. Every value in `payload` must be a real, already-committed fact —
    this module has no opinion on that beyond its own docstring; callers
    (db/claims.py, db/decisions.py, db/contradictions.py, agent/loop.py)
    are the ones that must never pass a fabricated number."""

    stream = _resolve_stream()
    if stream is None:
        return
    envelope = {
        "version": PROTOCOL_VERSION,
        "type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "payload": dict(payload),
    }
    line = json.dumps(envelope, default=str, sort_keys=True) + "\n"
    with _lock:
        stream.write(line)
        stream.flush()


def close_events_stream() -> None:
    """Tests call this between cases so a later test's CAIRN_EVENTS_FILE
    (or absence of one) isn't shadowed by a stale cached handle — mirrors
    db/pool.py::close_pool's role for the connection pool singleton."""

    global _stream, _stream_path
    if _stream is not None:
        _stream.close()
    _stream = None
    _stream_path = None
