"""Property tests for the jsonl-map/v1 FragmentAdapter (Appendix B.5, §18).

Pure/deterministic — no live cluster or S3 needed. The live "assembled root
byte-matches an independent no-cache run" proof is scripts/gate_c.sh, which
drives the real mapper subprocess against the live cluster; these tests
cover the adapter's own bucketing/digest/reduce/verify contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.adapters import jsonl_map
from cairn.flight.leaf_pipeline import globals_digest
from cairn.flight.types import (
    AdapterContract,
    ExecutionSpec,
    OutputContract,
    PlatformContract,
    PurityPolicy,
)

_MAPPER_DIGEST_A = "aa" * 32
_MAPPER_DIGEST_B = "bb" * 32


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _corpus(n: int) -> list[dict[str, object]]:
    return [{"id": f"row-{i:04d}", "text": f"document body {i}"} for i in range(n)]


def _spec(*, adapter: AdapterContract) -> ExecutionSpec:
    return ExecutionSpec(
        namespace_id="jsonl-leaves-test",
        argv=("python", "/workspace/examples/embed_mapper.py"),
        cwd_rel=".",
        output=OutputContract(path_rel=".cairn/out/features.jsonl"),
        platform=PlatformContract(os="linux", arch="amd64", image_digest="sha256:" + "cc" * 32),
        purity=PurityPolicy(
            contract_id="jsonl-map/v1",
            network="deny",
            clock="deny",
            random="deny",
            env_names=(),
            qualification_runs=2,
        ),
        adapter=adapter,
        tracer_version="strace/v1",
        companion_version="python-audit/v1",
        adapter_registry_version="adapters/v1",
    )


def _adapter(*, verifier_digest: str = "dd" * 32) -> AdapterContract:
    digest = "ee" * 32
    return AdapterContract(
        adapter_id="jsonl-map/v1",
        partitioner_digest=digest,
        reducer_digest=digest,
        verifier_digest=verifier_digest,
        partitions=64,
    )


def test_duplicate_stable_id_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    _write_jsonl(path, [{"id": "1", "text": "a"}, {"id": "1", "text": "b"}])
    with pytest.raises(jsonl_map.DuplicateIdError):
        jsonl_map.validate_input(path, "id")


def test_missing_id_field_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    _write_jsonl(path, [{"id": "1"}, {"not_id": "2"}])
    with pytest.raises(jsonl_map.MissingIdError):
        jsonl_map.validate_input(path, "id")


def test_bucket_assignment_is_deterministic() -> None:
    a = jsonl_map.canonical_id_bytes("row-0001")
    assert jsonl_map.bucket_of(a) == jsonl_map.bucket_of(a)
    buckets = {
        jsonl_map.bucket_of(jsonl_map.canonical_id_bytes(f"row-{i:04d}")) for i in range(200)
    }
    # 200 distinct ids over 64 buckets should spread across more than a
    # handful of buckets — catches an accidental constant-bucket bug.
    assert len(buckets) > 16


def _leaf_keys(manifest: jsonl_map.InputManifest, g_digest: str) -> dict[int, str]:
    keys = {}
    for b in range(64):
        sd = manifest.bucket_digest(b)
        keys[b] = jsonl_map.leaf_work_key(bucket=b, slice_digest_value=sd, globals_digest=g_digest)
    return keys


def _globals(mapper_digest: str) -> str:
    return globals_digest(
        spec=_spec(adapter=_adapter()), mapper_digest=mapper_digest, id_field="id"
    )


def test_changing_one_value_touches_exactly_one_bucket(tmp_path: Path) -> None:
    base = _corpus(50)
    path_a = tmp_path / "a.jsonl"
    _write_jsonl(path_a, base)
    manifest_a = jsonl_map.validate_input(path_a, "id")
    g = _globals(_MAPPER_DIGEST_A)
    keys_a = _leaf_keys(manifest_a, g)

    mutated = [dict(r) for r in base]
    mutated[10]["text"] = "a completely different document"
    path_b = tmp_path / "b.jsonl"
    _write_jsonl(path_b, mutated)
    manifest_b = jsonl_map.validate_input(path_b, "id")
    keys_b = _leaf_keys(manifest_b, g)

    changed = [b for b in range(64) if keys_a[b] != keys_b[b]]
    target_bucket = jsonl_map.bucket_of(jsonl_map.canonical_id_bytes(base[10]["id"]))
    assert changed == [target_bucket]


def test_adding_one_id_touches_exactly_one_bucket(tmp_path: Path) -> None:
    base = _corpus(50)
    path_a = tmp_path / "a.jsonl"
    _write_jsonl(path_a, base)
    manifest_a = jsonl_map.validate_input(path_a, "id")
    g = _globals(_MAPPER_DIGEST_A)
    keys_a = _leaf_keys(manifest_a, g)

    added = [*base, {"id": "row-9999", "text": "new document"}]
    path_b = tmp_path / "b.jsonl"
    _write_jsonl(path_b, added)
    manifest_b = jsonl_map.validate_input(path_b, "id")
    keys_b = _leaf_keys(manifest_b, g)

    changed = [b for b in range(64) if keys_a[b] != keys_b[b]]
    target_bucket = jsonl_map.bucket_of(jsonl_map.canonical_id_bytes("row-9999"))
    assert changed == [target_bucket]
    assert manifest_b.row_count == manifest_a.row_count + 1


def test_deleting_one_id_touches_exactly_one_bucket(tmp_path: Path) -> None:
    base = _corpus(50)
    path_a = tmp_path / "a.jsonl"
    _write_jsonl(path_a, base)
    manifest_a = jsonl_map.validate_input(path_a, "id")
    g = _globals(_MAPPER_DIGEST_A)
    keys_a = _leaf_keys(manifest_a, g)

    removed = [r for r in base if r["id"] != "row-0010"]
    path_b = tmp_path / "b.jsonl"
    _write_jsonl(path_b, removed)
    manifest_b = jsonl_map.validate_input(path_b, "id")
    keys_b = _leaf_keys(manifest_b, g)

    changed = [b for b in range(64) if keys_a[b] != keys_b[b]]
    target_bucket = jsonl_map.bucket_of(jsonl_map.canonical_id_bytes("row-0010"))
    assert changed == [target_bucket]
    assert manifest_b.row_count == manifest_a.row_count - 1


def test_replacing_an_id_touches_at_most_two_buckets(tmp_path: Path) -> None:
    base = _corpus(50)
    path_a = tmp_path / "a.jsonl"
    _write_jsonl(path_a, base)
    manifest_a = jsonl_map.validate_input(path_a, "id")
    g = _globals(_MAPPER_DIGEST_A)
    keys_a = _leaf_keys(manifest_a, g)

    replaced = [dict(r) for r in base]
    replaced[10]["id"] = "row-replaced"
    path_b = tmp_path / "b.jsonl"
    _write_jsonl(path_b, replaced)
    manifest_b = jsonl_map.validate_input(path_b, "id")
    keys_b = _leaf_keys(manifest_b, g)

    changed = {b for b in range(64) if keys_a[b] != keys_b[b]}
    old_bucket = jsonl_map.bucket_of(jsonl_map.canonical_id_bytes("row-0010"))
    new_bucket = jsonl_map.bucket_of(jsonl_map.canonical_id_bytes("row-replaced"))
    assert changed == {old_bucket, new_bucket}
    assert 1 <= len(changed) <= 2


def test_mapper_change_invalidates_all_64_leaf_keys(tmp_path: Path) -> None:
    base = _corpus(50)
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, base)
    manifest = jsonl_map.validate_input(path, "id")

    keys_a = _leaf_keys(manifest, _globals(_MAPPER_DIGEST_A))
    keys_b = _leaf_keys(manifest, _globals(_MAPPER_DIGEST_B))

    assert all(keys_a[b] != keys_b[b] for b in range(64))


def test_verifier_digest_change_invalidates_all_64_leaf_keys(tmp_path: Path) -> None:
    base = _corpus(50)
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, base)
    manifest = jsonl_map.validate_input(path, "id")

    spec_a = _spec(adapter=_adapter(verifier_digest="dd" * 32))
    spec_b = _spec(adapter=_adapter(verifier_digest="ff" * 32))
    g_a = globals_digest(spec=spec_a, mapper_digest=_MAPPER_DIGEST_A, id_field="id")
    g_b = globals_digest(spec=spec_b, mapper_digest=_MAPPER_DIGEST_A, id_field="id")

    keys_a = _leaf_keys(manifest, g_a)
    keys_b = _leaf_keys(manifest, g_b)
    assert all(keys_a[b] != keys_b[b] for b in range(64))


def test_reduce_is_globally_id_sorted_regardless_of_leaf_order() -> None:
    id_field = "id"

    def _leaf_bytes(rows: list[dict[str, object]]) -> bytes:
        lines = [json.dumps(r, sort_keys=True, separators=(",", ":")).encode() for r in rows]
        return b"\n".join(lines) + b"\n"

    leaf_a = _leaf_bytes([{"id": "z", "v": 1}, {"id": "m", "v": 2}])
    leaf_b = _leaf_bytes([{"id": "a", "v": 3}])

    forward = jsonl_map.reduce(id_field, [leaf_a, leaf_b])
    backward = jsonl_map.reduce(id_field, [leaf_b, leaf_a])
    assert forward == backward

    ids_in_order = [json.loads(line)["id"] for line in forward.splitlines() if line.strip()]
    assert ids_in_order == sorted(ids_in_order)
    assert ids_in_order == ["a", "m", "z"]


def test_verify_accepts_matching_bijection(tmp_path: Path) -> None:
    base = _corpus(5)
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, base)
    manifest = jsonl_map.validate_input(path, "id")

    assembled = (
        b"\n".join(
            json.dumps(
                {"id": r["id"], "embedding": [0.0]}, sort_keys=True, separators=(",", ":")
            ).encode()
            for r in base
        )
        + b"\n"
    )
    jsonl_map.verify(assembled, manifest)  # must not raise


def test_verify_rejects_missing_id(tmp_path: Path) -> None:
    base = _corpus(5)
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, base)
    manifest = jsonl_map.validate_input(path, "id")

    assembled = (
        b"\n".join(
            json.dumps(
                {"id": r["id"], "embedding": [0.0]}, sort_keys=True, separators=(",", ":")
            ).encode()
            for r in base[:-1]
        )
        + b"\n"
    )
    with pytest.raises(jsonl_map.JsonlMapError):
        jsonl_map.verify(assembled, manifest)


def test_verify_rejects_duplicate_id(tmp_path: Path) -> None:
    base = _corpus(5)
    path = tmp_path / "a.jsonl"
    _write_jsonl(path, base)
    manifest = jsonl_map.validate_input(path, "id")

    lines = [
        json.dumps(
            {"id": r["id"], "embedding": [0.0]}, sort_keys=True, separators=(",", ":")
        ).encode()
        for r in base
    ]
    lines.append(lines[0])
    assembled = b"\n".join(lines) + b"\n"
    with pytest.raises(jsonl_map.JsonlMapError):
        jsonl_map.verify(assembled, manifest)


def test_microchunks_are_fixed_size_eight() -> None:
    manifest_rows = [
        jsonl_map.Row(
            stable_id=str(i),
            id_bytes=jsonl_map.canonical_id_bytes(str(i)),
            line_bytes=json.dumps({"id": str(i)}).encode(),
        )
        for i in range(20)
    ]
    manifest_rows.sort(key=lambda r: r.id_bytes)
    chunks = list(jsonl_map.microchunks(0, manifest_rows))
    assert [len(c.rows) for c in chunks] == [8, 8, 4]
    assert [c.index for c in chunks] == [0, 1, 2]
