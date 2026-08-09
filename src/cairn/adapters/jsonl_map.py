"""The bundled ``jsonl-map/v1`` FragmentAdapter (Appendix B.5 + PLAN §18).

Bucket assignment, slice/leaf-key/microchunk-key digests, and the fixed
8-record microchunk policy for the one shipped partition/reducer contract.
Every digest is measured from real canonical bytes; nothing here invents a
digest, a leaf count, or a row count.

``validate_input`` runs before planning (not mid-run) so a duplicate or
missing stable ID is rejected before a single leaf is claimed — a duplicate
discovered mid-run would already have produced a wrong leaf (B.5).

Row placement: ``bucket_of`` hashes the record's *canonical typed id* — the
canonical JSON encoding of the id value, which round-trips ``str`` vs ``int``
without ambiguity — never the raw string form, so ``7`` and ``"7"`` are
different ids by construction (``ExecutionSpec``/``ResourceIdentity`` already
follow this "hash the canonical encoding, not the surface text" rule
elsewhere in ``flight/identity.py``).

Row *order* within a bucket also sorts by that same canonical-id byte
string. This is a deterministic total order (not a numeric/lexical "natural"
order) — the only property §18 requires of ``canonical_bucket_rows`` is that
every machine computing the same bucket produces the same byte sequence for
``slice_digest``, not that the order look sorted to a human.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from cairn.fingerprint.canon import canonical_json
from cairn.flight.types import Digest

ADAPTER_ID = "jsonl-map/v1"
PARTITIONS = 64
MICROCHUNK_SIZE = 8
MICROCHUNK_POLICY_VERSION = "cairn/jsonl-microchunk-policy/v1"
SCHEMA_VERSION = "cairn/jsonl-map/v1"


class JsonlMapError(ValueError):
    """Refused before planning — never a mid-run surprise."""


class DuplicateIdError(JsonlMapError):
    def __init__(self, stable_id: str, first_line: int, dup_line: int) -> None:
        super().__init__(f"duplicate stable id {stable_id!r} at lines {first_line} and {dup_line}")
        self.stable_id = stable_id


class MissingIdError(JsonlMapError):
    def __init__(self, line_no: int, id_field: str) -> None:
        super().__init__(f"row at line {line_no} is missing id field {id_field!r}")
        self.line_no = line_no


def _json_digest(label: str, body: object) -> Digest:
    """Domain-separated digest of a JSON-shaped payload — same recipe as
    ``flight/identity.py``'s ``_digest``, deliberately not a second encoder."""
    return sha256(canonical_json({"label": label, "v": SCHEMA_VERSION, "body": body})).hexdigest()


def _framed_digest(
    label: str, *, header_extra: dict[str, object], chunks: Sequence[bytes]
) -> Digest:
    """Digest of a length-prefixed byte stream, domain-separated by a
    canonical-JSON header. Rows are already canonical JSON bytes; re-wrapping
    them into a JSON array (base64-per-row) would work too but costs a copy
    for no identity benefit, so the bulk body is framed as raw
    ``len(8 bytes BE) || bytes`` records instead — the same length-prefix
    idea §18 uses for the mapper's own checkpoint frames."""
    header = canonical_json({"label": label, "v": SCHEMA_VERSION, **header_extra})
    h = sha256()
    h.update(len(header).to_bytes(8, "big"))
    h.update(header)
    for chunk in chunks:
        h.update(len(chunk).to_bytes(8, "big"))
        h.update(chunk)
    return h.hexdigest()


def canonical_id_bytes(value: object) -> bytes:
    """Canonical JSON encoding of a typed stable id — preserves str vs int."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise JsonlMapError(f"stable id must be str or int, got {type(value).__name__}")
    return canonical_json(value)


def bucket_of(id_bytes: bytes, *, partitions: int = PARTITIONS) -> int:
    digest = sha256(id_bytes).digest()
    return int.from_bytes(digest[:8], "big") % partitions


@dataclass(frozen=True, slots=True)
class Row:
    stable_id: object
    id_bytes: bytes
    line_bytes: bytes  # canonical JSON encoding of the full row object

    @property
    def bucket(self) -> int:
        return bucket_of(self.id_bytes)


@dataclass(frozen=True, slots=True)
class InputManifest:
    id_field: str
    row_count: int
    ids: tuple[bytes, ...]  # canonical id bytes, insertion order
    buckets: tuple[tuple[Row, ...], ...]  # index == bucket id, 0..partitions-1

    def bucket_digest(self, bucket: int) -> Digest:
        rows = self.buckets[bucket]
        return slice_digest(bucket, [r.line_bytes for r in rows])


def validate_input(path: Path, id_field: str, *, partitions: int = PARTITIONS) -> InputManifest:
    """Parse the JSONL input, reject dup/missing stable ids, bucket rows.

    Runs entirely before any leaf is planned or claimed.
    """
    seen: dict[bytes, int] = {}
    seen_display: dict[bytes, str] = {}
    buckets: list[list[Row]] = [[] for _ in range(partitions)]
    ids: list[bytes] = []
    row_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            obj: dict[str, Any] = json.loads(stripped)
            if id_field not in obj:
                raise MissingIdError(line_no, id_field)
            stable_id = obj[id_field]
            id_bytes = canonical_id_bytes(stable_id)
            if id_bytes in seen:
                raise DuplicateIdError(str(stable_id), seen[id_bytes], line_no)
            seen[id_bytes] = line_no
            seen_display[id_bytes] = str(stable_id)
            line_bytes = canonical_json(obj)
            row = Row(stable_id=stable_id, id_bytes=id_bytes, line_bytes=line_bytes)
            buckets[row.bucket].append(row)
            ids.append(id_bytes)
            row_count += 1
    sorted_buckets = tuple(
        tuple(sorted(bucket_rows, key=lambda r: r.id_bytes)) for bucket_rows in buckets
    )
    return InputManifest(
        id_field=id_field,
        row_count=row_count,
        ids=tuple(ids),
        buckets=sorted_buckets,
    )


def slice_digest(bucket: int, canonical_rows: Sequence[bytes]) -> Digest:
    return _framed_digest(
        "cairn/jsonl-slice/v1", header_extra={"bucket_id": bucket}, chunks=canonical_rows
    )


def leaf_work_key(*, bucket: int, slice_digest_value: Digest, globals_digest: Digest) -> Digest:
    """§18 leaf key: globals_digest stands in for the execution_spec + mapper
    + output_contract + global_resolved_nonrow_resource_set + image/platform/
    sanitized_environment + partitioner/reducer/verifier/microchunk_policy
    digests the orchestrator has already folded together once (identical
    across all 64 buckets); only ``bucket`` and ``slice_digest`` vary."""
    return _json_digest(
        "cairn/jsonl-leaf/v1",
        {"globals_digest": globals_digest, "bucket_id": bucket, "slice_digest": slice_digest_value},
    )


def merkle_root_digest(
    *, partitioner_digest: Digest, reducer_digest: Digest, leaves: Sequence[tuple[int, Digest]]
) -> Digest:
    ordered = sorted(leaves, key=lambda pair: pair[0])
    if [b for b, _ in ordered] != list(range(len(ordered))):
        raise JsonlMapError("merkle root requires every bucket 0..N-1 exactly once")
    return _json_digest(
        "cairn/jsonl-root/v1",
        {
            "partitioner_digest": partitioner_digest,
            "reducer_digest": reducer_digest,
            "leaves": [{"bucket_id": b, "child_blob_digest": d} for b, d in ordered],
        },
    )


@dataclass(frozen=True, slots=True)
class Microchunk:
    bucket: int
    index: int
    rows: tuple[Row, ...]
    input_digest: Digest


def microchunks(bucket: int, bucket_rows: Sequence[Row]) -> Iterator[Microchunk]:
    """Fixed 8-record chunks (§18) — ``microchunk_size`` is frozen for v1."""
    for start in range(0, len(bucket_rows), MICROCHUNK_SIZE):
        chunk_rows = tuple(bucket_rows[start : start + MICROCHUNK_SIZE])
        index = start // MICROCHUNK_SIZE
        digest = _framed_digest(
            "cairn/jsonl-microchunk-input/v1",
            header_extra={"bucket_id": bucket, "microchunk_index": index},
            chunks=[r.line_bytes for r in chunk_rows],
        )
        yield Microchunk(bucket=bucket, index=index, rows=chunk_rows, input_digest=digest)


def microchunk_key(leaf_key: Digest, index: int, input_digest: Digest) -> str:
    return _json_digest(
        "cairn/jsonl-microchunk/v1",
        {
            "leaf_semantic_work_key": leaf_key,
            "microchunk_index": index,
            "input_digest": input_digest,
        },
    )


def reduce(id_field: str, leaves: Sequence[bytes]) -> bytes:
    """Merge each leaf's already ID-sorted JSONL bytes into one globally
    ID-sorted output. Buckets are hash-assigned, not id-ordered, so the
    global order requires a merge across leaves, not a bucket-order concat."""
    records: list[tuple[bytes, bytes]] = []
    for leaf_bytes in leaves:
        for raw_line in leaf_bytes.splitlines():
            if not raw_line.strip():
                continue
            obj = json.loads(raw_line)
            if id_field not in obj:
                raise JsonlMapError(f"leaf output row missing id field {id_field!r}")
            records.append((canonical_id_bytes(obj[id_field]), canonical_json(obj)))
    records.sort(key=lambda pair: pair[0])
    return b"\n".join(line for _, line in records) + (b"\n" if records else b"")


def verify(assembled: bytes, manifest: InputManifest) -> None:
    """Bijection check: assembled output covers exactly the input id set,
    once each. Never a bytewise compare against a "clean" run here — that
    comparison is the gate script's job, this is the adapter's own contract."""
    seen: set[bytes] = set()
    for raw_line in assembled.splitlines():
        if not raw_line.strip():
            continue
        obj = json.loads(raw_line)
        if manifest.id_field not in obj:
            raise JsonlMapError("assembled output row missing id field")
        id_bytes = canonical_id_bytes(obj[manifest.id_field])
        if id_bytes in seen:
            raise JsonlMapError(f"assembled output has duplicate id {obj[manifest.id_field]!r}")
        seen.add(id_bytes)
    expected = set(manifest.ids)
    if seen != expected:
        missing = len(expected - seen)
        extra = len(seen - expected)
        raise JsonlMapError(
            f"assembled output id set does not match input: missing={missing} extra={extra}"
        )
