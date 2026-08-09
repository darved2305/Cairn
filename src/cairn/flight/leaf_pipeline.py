"""Day-4 stable Merkle leaf repair (PLAN §11, §18, Appendix B.5).

Bucket the frozen input into 64 stable leaves, restore whatever the
namespace already has current, and run the cooperative mapper only for
missing leaves — one project-controlled subprocess per leaf, checkpointed
every 8 records through the hardened ``commit_microchunk`` primitive. The
root publishes only after every current child leaf is re-verified in the
same transaction (``publish_derivation`` already does this via its
``leaves=`` parameter).

Leaves have no trace observation — a declared adapter contract is their
authority (``Authority.STRUCTURAL``), not an empirical trace. Only the
adapter/bucket/globals digests decide whether a leaf is reused.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from cairn.adapters import jsonl_map
from cairn.db import claims as claims_db
from cairn.db import flight as flight_db
from cairn.fingerprint.canon import canonical_json
from cairn.flight import identity as flight_identity
from cairn.flight.executor import compute_semantic_key, declared_file_inputs, environment_digest
from cairn.flight.restore import restore_output_atomic
from cairn.flight.types import Authority, Digest, ExecutionSpec
from cairn.obs.events import emit_event
from cairn.storage.cas import (
    BlobPublication,
    ContentBlobRef,
    fetch_blob_verified,
    publish_blob,
)

_SCHEMA = "cairn/jsonl-leaf-pipeline/v1"
MAX_FRAME_BYTES = 64 * 1024


class LeafPipelineError(RuntimeError):
    pass


def _digest(label: str, body: object) -> Digest:
    return hashlib.sha256(
        canonical_json({"label": label, "v": _SCHEMA, "body": body})
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class LeafOutcome:
    bucket: int
    leaf_key: Digest
    action: str  # "RESTORE" | "COMPUTED"
    derivation_id: uuid.UUID
    blob_digest: Digest
    row_count: int
    data: bytes  # canonical JSONL bytes, id-sorted, kept for root assembly


@dataclass(frozen=True, slots=True)
class RootOutcome:
    action: str  # "RESTORE" | "PUBLISHED"
    root_key: Digest
    derivation_id: uuid.UUID | None
    blob_digest: Digest
    merkle_root: Digest
    leaves: tuple[LeafOutcome, ...]
    output_path: Path


def globals_digest(
    *,
    spec: ExecutionSpec,
    mapper_digest: Digest,
    id_field: str,
) -> Digest:
    """Everything a leaf key needs beyond ``(bucket, slice_digest)`` — the
    same value for all 64 buckets, so computed once per run. §18: "Changing
    mapper code, any global resolved resource, bundle/image/platform/config,
    or output algebra changes all leaf keys" — every one of those inputs is
    folded in here."""
    assert spec.adapter is not None
    return _digest(
        "globals",
        {
            "spec_digest": flight_identity.spec_digest(spec),
            "output_contract_digest": flight_identity.output_contract_digest(spec),
            "platform_digest": flight_identity.platform_digest(spec),
            "environment_digest": environment_digest(spec.purity.env_names),
            "mapper_digest": mapper_digest,
            "id_field": id_field,
            "adapter": {
                "adapter_id": spec.adapter.adapter_id,
                "partitioner_digest": spec.adapter.partitioner_digest,
                "reducer_digest": spec.adapter.reducer_digest,
                "verifier_digest": spec.adapter.verifier_digest,
                "partitions": spec.adapter.partitions,
            },
            "microchunk_policy": jsonl_map.MICROCHUNK_POLICY_VERSION,
            "microchunk_size": jsonl_map.MICROCHUNK_SIZE,
        },
    )


def _read_frame(fd: int) -> dict[str, Any] | None:
    header = os.read(fd, 8)
    if len(header) < 8:
        return None
    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise LeafPipelineError(f"checkpoint frame length {length} out of bounds")
    body = b""
    remaining = length
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise LeafPipelineError("checkpoint pipe closed mid-frame")
        body += chunk
        remaining -= len(chunk)
    frame: dict[str, Any] = json.loads(body)
    if frame.get("protocol") != "cairn-checkpoint/v1":
        raise LeafPipelineError(f"unknown checkpoint protocol {frame.get('protocol')!r}")
    return frame


def _run_mapper_for_leaf(
    *,
    bucket: int,
    rows: tuple[jsonl_map.Row, ...],
    expected_chunks: list[jsonl_map.Microchunk],
    id_field: str,
    mapper_path: Path,
    workspace: Path,
    leaf_key: Digest,
    already_committed: dict[int, Digest],
    pool: ConnectionPool,
    namespace_id: str,
    generation: int,
    run_id: uuid.UUID,
    fence: int,
    owner_id: str,
    bucket_dir: Path,
    s3_bucket: str,
) -> dict[int, tuple[BlobPublication, bytes]]:
    """Launch the cooperative mapper, validate each checkpoint frame, and
    commit it through the fenced ``commit_microchunk`` primitive. Returns
    newly-committed {index: (publication, bytes)} — resumed indices are not
    re-uploaded here, the caller already has their blob_digest."""
    input_slice = bucket_dir / "input_slice.jsonl"
    input_slice.write_text(
        "\n".join(row.line_bytes.decode("utf-8") for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    checkpoint_dir = bucket_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    resume_path = bucket_dir / "resume_manifest.json"
    resume_path.write_text(
        json.dumps(
            {
                "committed": [
                    {"microchunk_index": idx, "input_digest": digest}
                    for idx, digest in already_committed.items()
                ]
            }
        ),
        encoding="utf-8",
    )

    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)

    env = dict(os.environ)
    env["CAIRN_INPUT_SLICE"] = str(input_slice)
    # The real fd number pass_fds preserves across exec, not a hardcoded 3:
    # a hardcoded target would need dup2 via preexec_fn, which forces
    # subprocess.Popen onto the fork() path instead of posix_spawn. fork()
    # in a process holding an open psycopg ConnectionPool (background
    # maintenance thread, live sockets) is unsafe — only the calling thread
    # survives into the child, so the pool's own locks/fds can end up in an
    # inconsistent state that corrupts the *parent* pool once the child
    # exits ("Bad file descriptor" on the next pool operation). Passing the
    # fd under its own number needs no preexec_fn, so Popen can use
    # posix_spawn, which never forks this process at all.
    env["CAIRN_CHECKPOINT_FD"] = str(write_fd)
    env["CAIRN_CHECKPOINT_DIR"] = str(checkpoint_dir)
    env["CAIRN_RESUME_MANIFEST"] = str(resume_path)
    env["CAIRN_BUCKET"] = str(bucket)
    argv = [sys.executable, str(mapper_path), "--leaf", "--id-field", id_field]

    if sys.platform == "win32":
        # Windows has no fd-inheritance-by-number equivalent; the leaf
        # mapper protocol is POSIX-only for v1 — the same platform boundary
        # Gate A/B already draw for verified execution. Callers must run
        # this inside the digest-pinned Linux worker.
        raise LeafPipelineError(
            "leaf pipeline requires POSIX checkpoint-fd wiring; run inside the Linux worker"
        )

    proc = subprocess.Popen(
        argv,
        cwd=str(workspace),
        env=env,
        pass_fds=(write_fd,),
        close_fds=True,
    )
    os.close(write_fd)

    expected: dict[int, Digest] = {c.index: c.input_digest for c in expected_chunks}
    newly_committed: dict[int, tuple[BlobPublication, bytes]] = {}
    try:
        while True:
            frame = _read_frame(read_fd)
            if frame is None:
                break
            index = int(frame["chunk_index"])
            input_digest = str(frame["input_digest"])
            temp_relpath = str(frame["temp_relpath"])
            claimed_digest = str(frame["claimed_output_digest"])

            if index in already_committed or index in newly_committed:
                continue
            if index not in expected:
                raise LeafPipelineError(f"mapper emitted unknown chunk index {index}")
            if expected[index] != input_digest:
                raise LeafPipelineError(
                    f"chunk {index} input_digest mismatch: expected {expected[index]}, "
                    f"mapper claims {input_digest}"
                )
            if "/" in temp_relpath or "\\" in temp_relpath or ".." in temp_relpath:
                raise LeafPipelineError(f"unsafe checkpoint temp_relpath {temp_relpath!r}")
            chunk_path = checkpoint_dir / temp_relpath
            if not chunk_path.is_file() or chunk_path.is_symlink():
                raise LeafPipelineError(f"checkpoint path is not a regular file: {chunk_path}")
            body = chunk_path.read_bytes()
            actual_digest = hashlib.sha256(body).hexdigest()
            if actual_digest != claimed_digest:
                raise LeafPipelineError(
                    f"chunk {index} claimed digest {claimed_digest} but re-hashed to {actual_digest}"
                )

            publication = publish_blob(s3_bucket, body)
            outcome = flight_db.commit_microchunk(
                pool,
                namespace_id=namespace_id,
                semantic_work_key=leaf_key,
                generation=generation,
                microchunk_key=jsonl_map.microchunk_key(leaf_key, index, input_digest),
                input_slice_digest=input_digest,
                blob=publication,
                run_id=run_id,
                fence=fence,
                owner_id=owner_id,
            )
            if outcome is flight_db.MicrochunkOutcome.NONDETERMINISTIC:
                raise LeafPipelineError(f"chunk {index} nondeterministic across attempts")
            if outcome is flight_db.MicrochunkOutcome.REJECTED_FENCE:
                raise LeafPipelineError(f"lost fence while committing chunk {index}")
            newly_committed[index] = (publication, body)
    except BaseException:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
        with contextlib.suppress(OSError):
            os.close(read_fd)
        raise
    else:
        exit_code = proc.wait(timeout=600)
        with contextlib.suppress(OSError):
            os.close(read_fd)

    if exit_code != 0:
        raise LeafPipelineError(f"mapper for bucket {bucket} exited {exit_code}")
    missing = set(expected) - set(already_committed) - set(newly_committed)
    if missing:
        raise LeafPipelineError(f"bucket {bucket} missing committed chunks: {sorted(missing)}")
    return newly_committed


def run_leaf_pipeline(
    pool: ConnectionPool,
    *,
    spec: ExecutionSpec,
    input_file: Path,
    id_field: str,
    mapper_path: Path,
    output_file: Path,
    workspace: Path,
    bucket: str,
    owner_id: str,
    host: str,
    region: str,
) -> RootOutcome:
    assert spec.adapter is not None
    expected_output = (workspace / spec.output.path_rel).resolve()
    if output_file.resolve() != expected_output:
        raise LeafPipelineError(
            f"output_file {output_file} does not match spec.output.path_rel {expected_output}"
        )
    partitions = spec.adapter.partitions

    manifest = jsonl_map.validate_input(input_file, id_field, partitions=partitions)
    mapper_digest = hashlib.sha256(mapper_path.read_bytes()).hexdigest()
    g_digest = globals_digest(spec=spec, mapper_digest=mapper_digest, id_field=id_field)

    leaf_keys: list[Digest] = []
    slice_digests: dict[int, Digest] = {}
    for b in range(partitions):
        sd = manifest.bucket_digest(b)
        slice_digests[b] = sd
        leaf_keys.append(
            jsonl_map.leaf_work_key(bucket=b, slice_digest_value=sd, globals_digest=g_digest)
        )

    current = flight_db.current_derivations(pool, namespace_id=spec.namespace_id, keys=leaf_keys)

    outcomes: dict[int, LeafOutcome] = {}
    with tempfile.TemporaryDirectory(prefix="cairn-leaf-") as tmp_root:
        tmp_path = Path(tmp_root)
        for b in range(partitions):
            leaf_key = leaf_keys[b]
            existing = current.get(leaf_key)
            if existing is not None:
                ref = ContentBlobRef(
                    blob_digest=existing.blob_digest,
                    bucket=existing.bucket,
                    object_key=existing.object_key,
                    version_id=existing.version_id,
                    checksum_sha256=existing.checksum_sha256,
                    size_bytes=existing.size_bytes,
                )
                data = fetch_blob_verified(ref)
                outcomes[b] = LeafOutcome(
                    bucket=b,
                    leaf_key=leaf_key,
                    action="RESTORE",
                    derivation_id=existing.derivation_id,
                    blob_digest=existing.blob_digest,
                    row_count=len(manifest.buckets[b]),
                    data=data,
                )
                continue

            run_id = uuid.uuid4()
            request_id = uuid.uuid4()
            claim = flight_db.open_generation(
                pool,
                namespace_id=spec.namespace_id,
                semantic_work_key=leaf_key,
                stage_label="leaf",
                owner_id=owner_id,
                host=host,
                region=region,
                run_id=run_id,
                request_id=request_id,
            )

            if claim.role is flight_db.ClaimRole.RESTORE and claim.derivation_id is not None:
                refreshed = flight_db.current_derivations(
                    pool, namespace_id=spec.namespace_id, keys=[leaf_key]
                ).get(leaf_key)
                if refreshed is None:
                    raise LeafPipelineError(f"leaf {b} claim points at unreachable derivation")
                ref = ContentBlobRef(
                    blob_digest=refreshed.blob_digest,
                    bucket=refreshed.bucket,
                    object_key=refreshed.object_key,
                    version_id=refreshed.version_id,
                    checksum_sha256=refreshed.checksum_sha256,
                    size_bytes=refreshed.size_bytes,
                )
                data = fetch_blob_verified(ref)
                outcomes[b] = LeafOutcome(
                    bucket=b,
                    leaf_key=leaf_key,
                    action="RESTORE",
                    derivation_id=refreshed.derivation_id,
                    blob_digest=refreshed.blob_digest,
                    row_count=len(manifest.buckets[b]),
                    data=data,
                )
                continue

            if claim.role is flight_db.ClaimRole.SUBSCRIBER:
                try:
                    sub = claims_db.subscribe(pool, claim.claim_key)
                except KeyboardInterrupt:
                    # Ctrl-C detaches this caller's own interest without
                    # cancelling the owner's shared work — the interest row
                    # `open_generation` already registered for this
                    # request_id is what makes last-subscriber cancellation
                    # computable; leaving it LIVE forever after we walk away
                    # would starve that check.
                    flight_db.detach_interest(
                        pool,
                        namespace_id=spec.namespace_id,
                        semantic_work_key=leaf_key,
                        generation=claim.generation,
                        subscriber_id=request_id,
                    )
                    raise
                refreshed = flight_db.current_derivations(
                    pool, namespace_id=spec.namespace_id, keys=[leaf_key]
                ).get(leaf_key)
                if refreshed is None:
                    raise LeafPipelineError(
                        f"leaf {b} owner reached {sub.terminal_state} without a current derivation"
                    )
                ref = ContentBlobRef(
                    blob_digest=refreshed.blob_digest,
                    bucket=refreshed.bucket,
                    object_key=refreshed.object_key,
                    version_id=refreshed.version_id,
                    checksum_sha256=refreshed.checksum_sha256,
                    size_bytes=refreshed.size_bytes,
                )
                data = fetch_blob_verified(ref)
                outcomes[b] = LeafOutcome(
                    bucket=b,
                    leaf_key=leaf_key,
                    action="RESTORE",
                    derivation_id=refreshed.derivation_id,
                    blob_digest=refreshed.blob_digest,
                    row_count=len(manifest.buckets[b]),
                    data=data,
                )
                continue

            # OWNER or TAKE_OVER: compute this leaf.
            assert claim.fence is not None
            bucket_dir = tmp_path / f"bucket-{b:02d}"
            bucket_dir.mkdir(parents=True, exist_ok=True)
            bucket_rows = manifest.buckets[b]
            expected_chunks = list(jsonl_map.microchunks(b, bucket_rows))

            committed_rows = flight_db.list_fragment_commits(
                pool,
                namespace_id=spec.namespace_id,
                semantic_work_key=leaf_key,
                generation=claim.generation,
            )
            # fragment_commits.input_slice_digest holds the *microchunk's*
            # own input_digest (commit_microchunk's parameter name), not the
            # leaf-level bucket slice_digest — match resumed chunks by that
            # digest rather than trying to decode the opaque microchunk_key.
            committed_blob_by_input_digest = {digest: blob for _k, digest, blob in committed_rows}
            already_committed = {
                c.index: c.input_digest
                for c in expected_chunks
                if c.input_digest in committed_blob_by_input_digest
            }

            newly = _run_mapper_for_leaf(
                bucket=b,
                rows=bucket_rows,
                expected_chunks=expected_chunks,
                id_field=id_field,
                mapper_path=mapper_path,
                workspace=workspace,
                leaf_key=leaf_key,
                already_committed=already_committed,
                pool=pool,
                namespace_id=spec.namespace_id,
                generation=claim.generation,
                run_id=run_id,
                fence=claim.fence,
                owner_id=owner_id,
                bucket_dir=bucket_dir,
                s3_bucket=bucket,
            )

            ordered_bodies: list[bytes] = []
            for idx in range(len(expected_chunks)):
                if idx in newly:
                    ordered_bodies.append(newly[idx][1])
                else:
                    input_digest = already_committed[idx]
                    blob_digest = committed_blob_by_input_digest[input_digest]
                    blob_ref = flight_db.get_content_blob(pool, blob_digest=blob_digest)
                    if blob_ref is None:
                        raise LeafPipelineError(f"resumed chunk {idx} blob missing/invalid")
                    ordered_bodies.append(
                        fetch_blob_verified(
                            ContentBlobRef(
                                blob_digest=blob_ref.blob_digest,
                                bucket=blob_ref.bucket,
                                object_key=blob_ref.object_key,
                                version_id=blob_ref.version_id,
                                checksum_sha256=blob_ref.checksum_sha256,
                                size_bytes=blob_ref.size_bytes,
                            )
                        )
                    )
            leaf_bytes = b"".join(ordered_bodies)

            leaf_publication = publish_blob(bucket, leaf_bytes)
            derivation_id = uuid.uuid4()
            pub_op = uuid.uuid4()
            outcome = flight_db.publish_derivation(
                pool,
                namespace_id=spec.namespace_id,
                semantic_work_key=leaf_key,
                generation=claim.generation,
                derivation_id=derivation_id,
                publication_operation_id=pub_op,
                blob=leaf_publication,
                observation_id=None,
                authority=Authority.STRUCTURAL,
                run_id=run_id,
                owner_id=owner_id,
                fence=claim.fence,
                stage_label="leaf",
                decision_explanation=(
                    f"jsonl-map/v1 leaf {b} computed under declared adapter contract"
                ),
            )
            if outcome not in (
                flight_db.PublishOutcome.PUBLISHED,
                flight_db.PublishOutcome.ALREADY_PRESENT,
            ):
                refreshed = flight_db.current_derivations(
                    pool, namespace_id=spec.namespace_id, keys=[leaf_key]
                ).get(leaf_key)
                if refreshed is None:
                    raise LeafPipelineError(f"leaf {b} publish failed: {outcome.value}")
                ref = ContentBlobRef(
                    blob_digest=refreshed.blob_digest,
                    bucket=refreshed.bucket,
                    object_key=refreshed.object_key,
                    version_id=refreshed.version_id,
                    checksum_sha256=refreshed.checksum_sha256,
                    size_bytes=refreshed.size_bytes,
                )
                data = fetch_blob_verified(ref)
                outcomes[b] = LeafOutcome(
                    bucket=b,
                    leaf_key=leaf_key,
                    action="RESTORE",
                    derivation_id=refreshed.derivation_id,
                    blob_digest=refreshed.blob_digest,
                    row_count=len(bucket_rows),
                    data=data,
                )
                continue

            outcomes[b] = LeafOutcome(
                bucket=b,
                leaf_key=leaf_key,
                action="COMPUTED",
                derivation_id=derivation_id,
                blob_digest=leaf_publication.blob_digest,
                row_count=len(bucket_rows),
                data=leaf_bytes,
            )

    ordered_leaves = tuple(outcomes[b] for b in range(partitions))
    assembled = jsonl_map.reduce(id_field, [leaf.data for leaf in ordered_leaves])
    jsonl_map.verify(assembled, manifest)

    merkle_root = jsonl_map.merkle_root_digest(
        partitioner_digest=spec.adapter.partitioner_digest,
        reducer_digest=spec.adapter.reducer_digest,
        leaves=[(leaf.bucket, leaf.blob_digest) for leaf in ordered_leaves],
    )

    resolved = declared_file_inputs([input_file], workspace=workspace)
    root_key = compute_semantic_key(spec, resolved)

    out_path = (workspace / spec.output.path_rel).resolve()
    run_id = uuid.uuid4()
    request_id = uuid.uuid4()
    root_claim = flight_db.open_generation(
        pool,
        namespace_id=spec.namespace_id,
        semantic_work_key=root_key,
        stage_label="root",
        owner_id=owner_id,
        host=host,
        region=region,
        run_id=run_id,
        request_id=request_id,
    )

    if root_claim.role is flight_db.ClaimRole.RESTORE and root_claim.derivation_id is not None:
        refreshed = flight_db.current_derivations(
            pool, namespace_id=spec.namespace_id, keys=[root_key]
        ).get(root_key)
        if refreshed is not None:
            ref = ContentBlobRef(
                blob_digest=refreshed.blob_digest,
                bucket=refreshed.bucket,
                object_key=refreshed.object_key,
                version_id=refreshed.version_id,
                checksum_sha256=refreshed.checksum_sha256,
                size_bytes=refreshed.size_bytes,
            )
            data = fetch_blob_verified(ref)
            restore_output_atomic(out_path, data)
            return RootOutcome(
                action="RESTORE",
                root_key=root_key,
                derivation_id=refreshed.derivation_id,
                blob_digest=refreshed.blob_digest,
                merkle_root=merkle_root,
                leaves=ordered_leaves,
                output_path=out_path,
            )

    leaf_edges = [
        flight_db.LeafEdge(
            partition_key=str(leaf.bucket),
            ordinal=leaf.bucket,
            child_derivation_id=leaf.derivation_id,
            input_slice_digest=slice_digests[leaf.bucket],
        )
        for leaf in ordered_leaves
    ]
    root_publication = publish_blob(bucket, assembled)
    root_derivation_id = uuid.uuid4()
    pub_op = uuid.uuid4()
    assert root_claim.fence is not None
    outcome = flight_db.publish_derivation(
        pool,
        namespace_id=spec.namespace_id,
        semantic_work_key=root_key,
        generation=root_claim.generation,
        derivation_id=root_derivation_id,
        publication_operation_id=pub_op,
        blob=root_publication,
        observation_id=None,
        authority=Authority.STRUCTURAL,
        run_id=run_id,
        owner_id=owner_id,
        fence=root_claim.fence,
        leaves=leaf_edges,
        stage_label="root",
        decision_explanation="jsonl-map/v1 root assembled from 64 leaves",
    )
    if outcome not in (flight_db.PublishOutcome.PUBLISHED, flight_db.PublishOutcome.ALREADY_PRESENT):
        raise LeafPipelineError(f"root publish failed: {outcome.value}")

    flight_db.record_composite_derivation(
        pool,
        parent_derivation_id=root_derivation_id,
        adapter_id=spec.adapter.adapter_id,
        partitioner_digest=spec.adapter.partitioner_digest,
        reducer_digest=spec.adapter.reducer_digest,
        verifier_digest=spec.adapter.verifier_digest,
        merkle_root_digest=merkle_root,
        leaf_count=partitions,
        output_metadata={
            "row_count": manifest.row_count,
            "reused_leaves": sum(1 for leaf in ordered_leaves if leaf.action == "RESTORE"),
            "computed_leaves": sum(1 for leaf in ordered_leaves if leaf.action == "COMPUTED"),
        },
    )

    restore_output_atomic(out_path, assembled)
    emit_event(
        "flight.leaf_pipeline",
        {
            "namespace_id": spec.namespace_id,
            "root_key": root_key,
            "merkle_root": merkle_root,
            "reused_leaves": sum(1 for leaf in ordered_leaves if leaf.action == "RESTORE"),
            "computed_leaves": sum(1 for leaf in ordered_leaves if leaf.action == "COMPUTED"),
        },
        run_id=str(run_id),
    )
    return RootOutcome(
        action="PUBLISHED",
        root_key=root_key,
        derivation_id=root_derivation_id,
        blob_digest=root_publication.blob_digest,
        merkle_root=merkle_root,
        leaves=ordered_leaves,
        output_path=out_path,
    )

