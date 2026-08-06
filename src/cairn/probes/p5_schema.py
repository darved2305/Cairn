"""P5 schema_stats — PROJECT.md §4.4.

Proves: schema (names, dtypes, order) and row count are identical, and a
per-column checksum over a deterministic 10% partition slice matches.

Does not prove: content equality outside the slice.
"""

from __future__ import annotations

import hashlib

import pyarrow as pa
import pyarrow.parquet as pq

from cairn.fingerprint.canon import canonical_json
from cairn.probes.base import CONSERVATIVE_TOLERANCE, ProbeResult, Timer, evidence_digest

PROBE_TYPE = "P5"
SLICE_FRACTION = 0.10


def _slice_indices(n: int, fraction: float) -> list[int]:
    """Every Nth row, deterministic and independent of any artifact_id —
    schema/content conformance isn't a per-artifact hash-selection concern
    the way P3/P4's row sampling is; a fixed stride is simpler and just as
    reproducible."""
    if n == 0:
        return []
    stride = max(1, int(1 / fraction))
    return list(range(0, n, stride))


def _table_checksum(table: pa.Table) -> str:
    parts = []
    for name in sorted(table.column_names):
        values = table.column(name).combine_chunks().to_pylist()
        parts.append((name, hashlib.sha256(canonical_json(values)).hexdigest()))
    return hashlib.sha256(canonical_json(parts)).hexdigest()


def run(
    old_parquet_bytes: bytes, new_parquet_bytes: bytes, *, slice_fraction: float = SLICE_FRACTION
) -> ProbeResult:
    with Timer() as t:
        old_table = pq.read_table(pa.BufferReader(old_parquet_bytes))
        new_table = pq.read_table(pa.BufferReader(new_parquet_bytes))

        schema_ok = old_table.schema.equals(new_table.schema)
        count_ok = old_table.num_rows == new_table.num_rows

        if not (schema_ok and count_ok):
            passed = False
            old_checksum = new_checksum = ""
            indices: list[int] = []
        else:
            indices = _slice_indices(old_table.num_rows, slice_fraction)
            old_checksum = _table_checksum(old_table.take(indices)) if indices else ""
            new_checksum = _table_checksum(new_table.take(indices)) if indices else ""
            passed = old_checksum == new_checksum

    detail = "ok"
    if not schema_ok:
        detail = f"schema differs: {old_table.schema} vs {new_table.schema}"
    elif not count_ok:
        detail = f"row count differs: {old_table.num_rows} vs {new_table.num_rows}"
    elif not passed:
        detail = "slice checksum differs"

    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec=f"schema + row count + checksum over every {max(1, int(1 / slice_fraction))}th row",
        population_size=old_table.num_rows,
        sample_size=len(indices),
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(old_checksum, new_checksum),
        detail=detail,
    )
