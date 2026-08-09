"""Linux Flight Recorder evidence collector (Appendix C).

The kernel process-tree collector is the coverage boundary. The Python
companion may only add rows and refine refs — never upgrade coverage.
"""

from __future__ import annotations

from cairn.trace.collector import (
    TRACER_VERSION,
    CollectorResult,
    RawTraceEvent,
    collect,
    parse_strace_line,
)
from cairn.trace.normalize import (
    normalize_trace,
    semantic_resource_set,
)
from cairn.trace.scout import ScoutResult, run_scout

__all__ = [
    "TRACER_VERSION",
    "CollectorResult",
    "RawTraceEvent",
    "ScoutResult",
    "collect",
    "normalize_trace",
    "parse_strace_line",
    "run_scout",
    "semantic_resource_set",
]
