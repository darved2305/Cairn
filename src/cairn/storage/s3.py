"""Content-addressed artifact storage and fragment IO.

docs/project/PROJECT.md §4.2 idempotency: completion writes to a content-addressed S3
key, so replaying a write is a genuine no-op — put_content_addressed is
what makes that true at the storage layer, not just at the database row.

New microchunks publish under ``cas/sha256/<digest>`` via
``storage.cas.publish_blob`` (conditional ``If-None-Match: *``). The
overwriteable ``fragments/{work_key}/{index}.bin`` key below remains only
for the five-stage compatibility path: a dispossessed owner must not be
able to overwrite Flight Recorder microchunks after takeover.

Works against real AWS S3 or a local S3-compatible endpoint (MinIO) via
CAIRN_S3_ENDPOINT_URL — same trick D1/D2 used to test the claim protocol
against local CockroachDB instead of waiting on cloud credentials.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

_CLIENT: Any = None

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def get_client() -> Any:
    """The process-wide S3 client, opened lazily. Mirrors db.pool.get_pool:
    one client per process, not per call site."""
    global _CLIENT
    if _CLIENT is None:
        # Keep boto3 lazy.  On Windows, psycopg-binary and botocore can load
        # incompatible native OpenSSL DLLs into the same interpreter.  This
        # host fails hard (OPENSSL_Uplink / no OPENSSL_Applink) rather than
        # raising a Python exception, so merely importing boto3 eagerly is
        # enough to make the real workload fragile.
        import boto3
        from botocore.config import Config

        kwargs: dict[str, Any] = {
            "config": Config(retries={"max_attempts": 5, "mode": "standard"}),
        }
        endpoint = os.environ.get("CAIRN_S3_ENDPOINT_URL")
        if endpoint:
            # MinIO / local dev — path-style addressing, dummy creds are fine.
            kwargs["endpoint_url"] = endpoint
            kwargs["aws_access_key_id"] = os.environ.get("CAIRN_S3_ACCESS_KEY", "minioadmin")
            kwargs["aws_secret_access_key"] = os.environ.get("CAIRN_S3_SECRET_KEY", "minioadmin")
        _CLIENT = boto3.client("s3", **kwargs)
    return _CLIENT


def _aws_cli_path() -> str | None:
    """Use AWS CLI as an isolated S3 transport on affected Windows hosts.

    The CLI is a separate process with its own TLS runtime, so it cannot
    collide with psycopg's OpenSSL DLLs. Local MinIO remains on boto3 because
    tests need the configured endpoint and do not open a real CockroachDB TLS
    connection in the same process.
    """
    if sys.platform != "win32" or os.environ.get("CAIRN_S3_ENDPOINT_URL"):
        return None
    return shutil.which("aws")


def _run_aws_cli(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    executable = _aws_cli_path()
    if executable is None:
        raise RuntimeError("AWS CLI isolation requested but aws is not available on PATH")
    completed = subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed


def _cli_error(operation: str, completed: subprocess.CompletedProcess[str]) -> RuntimeError:
    detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
    return RuntimeError(
        f"AWS CLI S3 {operation} failed (exit {completed.returncode}): {detail[:500]}"
    )


def _cli_put(bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cairn-s3-put-", suffix=".bin", delete=False) as f:
            f.write(data)
            temp_path = Path(f.name)
        args = [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(temp_path),
            "--output",
            "json",
        ]
        if content_type is not None:
            args.extend(["--content-type", content_type])
        completed = _run_aws_cli(args)
        if completed.returncode != 0:
            raise _cli_error("put-object", completed)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _cli_get(bucket: str, key: str) -> bytes:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cairn-s3-get-", suffix=".bin", delete=False) as f:
            temp_path = Path(f.name)
        completed = _run_aws_cli(
            [
                "s3api",
                "get-object",
                "--bucket",
                bucket,
                "--key",
                key,
                str(temp_path),
                "--output",
                "json",
            ]
        )
        if completed.returncode != 0:
            raise _cli_error("get-object", completed)
        return temp_path.read_bytes()
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def close_client() -> None:
    """Forget the process-wide client. Tests call this between cases."""
    global _CLIENT
    _CLIENT = None


def content_address(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PutResult:
    content_digest: str
    key: str
    s3_uri: str
    already_existed: bool


def _checksum_sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _cli_put_if_none_match(
    bucket: str, key: str, data: bytes, content_type: str | None = None
) -> None:
    """Conditional PUT via AWS CLI. Raises ClientError-shaped failure on 412."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="cairn-s3-put-", suffix=".bin", delete=False) as f:
            f.write(data)
            temp_path = Path(f.name)
        args = [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(temp_path),
            "--if-none-match",
            "*",
            "--checksum-sha256",
            _checksum_sha256_b64(data),
            "--checksum-algorithm",
            "SHA256",
            "--output",
            "json",
        ]
        if content_type is not None:
            args.extend(["--content-type", content_type])
        completed = _run_aws_cli(args)
        if completed.returncode != 0:
            detail = f"{completed.stderr}\n{completed.stdout}"
            if "PreconditionFailed" in detail or "412" in detail:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": detail[:500]},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            raise _cli_error("put-object", completed)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _put_if_none_match(bucket: str, key: str, data: bytes, *, content_type: str) -> None:
    """Atomic first-writer-wins PUT. No HEAD-then-PUT race."""
    checksum = _checksum_sha256_b64(data)
    if _aws_cli_path() is not None:
        _cli_put_if_none_match(bucket, key, data, content_type)
        return
    get_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        IfNoneMatch="*",
        ChecksumSHA256=checksum,
        ChecksumAlgorithm="SHA256",
    )


def _is_precondition_failed(exc: BaseException) -> bool:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in {"PreconditionFailed", "412"}
    return "PreconditionFailed" in str(exc) or "412" in str(exc)


def put_content_addressed(
    bucket: str, prefix: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> PutResult:
    """Idempotent by construction: the key IS the content hash.

    Uses ``If-None-Match: *`` so two concurrent first writers cannot race a
    HEAD-then-unconditional-PUT. On 412, fetch the existing object, rehash,
    and accept only when the stored bytes match this digest — a mismatch is
    a bucket integrity failure, not a successful no-op.
    """
    digest = content_address(data)
    key = f"{prefix}/{digest}"
    try:
        _put_if_none_match(bucket, key, data, content_type=content_type)
        return PutResult(digest, key, f"s3://{bucket}/{key}", already_existed=False)
    except Exception as exc:
        if not _is_precondition_failed(exc):
            raise
        # Object already present — verify bytes before claiming success.
        existing = get_bytes(bucket, key)
        actual = content_address(existing)
        if actual != digest:
            raise RuntimeError(
                f"412 at s3://{bucket}/{key} but stored bytes hash to {actual}, "
                f"expected {digest} — bucket integrity failure"
            ) from exc
        return PutResult(digest, key, f"s3://{bucket}/{key}", already_existed=True)


def get_bytes(bucket: str, key: str) -> bytes:
    if _aws_cli_path() is not None:
        return _cli_get(bucket, key)
    body: bytes = get_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    return body


def put_bytes(
    bucket: str, key: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> str:
    """Fixed-key write for vendored reference data (dataset/model
    snapshots) addressed by a stable name, not by content — always
    overwrites. Callers that need idempotency want put_content_addressed."""
    if _aws_cli_path() is not None:
        _cli_put(bucket, key, data, content_type)
    else:
        get_client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return f"s3://{bucket}/{key}"


def fragment_key(work_key: str, fragment_index: int) -> str:
    """Overwriteable fixed key — five-stage compatibility path ONLY.

    New Flight Recorder microchunks must use ``cas.publish_blob`` under
    ``cas/sha256/<digest>``. A dispossessed owner that still holds this
    mutable name can overwrite bytes after takeover; do not extend this.
    """
    return f"fragments/{work_key}/{fragment_index:04d}.bin"


def put_fragment(bucket: str, work_key: str, fragment_index: int, data: bytes) -> tuple[str, str]:
    """Five-stage compatibility path only. Returns (s3_uri, content_digest).

    New microchunks must call ``cas.publish_blob`` instead. This still writes
    the mutable ``fragments/...`` key so existing checkpoint resume keeps
    working until that path is routed through CAS + fragment_commits.
    """
    key = fragment_key(work_key, fragment_index)
    digest = content_address(data)
    if _aws_cli_path() is not None:
        _cli_put(bucket, key, data)
    else:
        get_client().put_object(Bucket=bucket, Key=key, Body=data)
    return f"s3://{bucket}/{key}", digest


def get_fragment_verified(
    bucket: str, work_key: str, fragment_index: int, expected_digest: str
) -> bytes:
    """Fetch a fragment and verify it against the digest run_fragments
    recorded before trusting it. A mismatch means the object was
    corrupted or overwritten out from under us — fail loudly rather than
    resume a checkpoint/features shard from bad data."""
    data = get_bytes(bucket, fragment_key(work_key, fragment_index))
    actual = content_address(data)
    if actual != expected_digest:
        raise ValueError(
            f"fragment work_key={work_key!r} index={fragment_index} content digest "
            f"mismatch: expected {expected_digest}, got {actual}"
        )
    return data
