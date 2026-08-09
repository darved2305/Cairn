"""Content-addressed artifact storage and fragment IO.

PROJECT.md §4.2 idempotency: completion writes to a content-addressed S3
key, so replaying a write is a genuine no-op — put_content_addressed is
what makes that true at the storage layer, not just at the database row.

PROJECT.md §4.5 crash recovery: fragments use a *predictable* key (a
resuming worker has to find them before it knows what's in them) and are
integrity-checked against a separately recorded content_digest, not
addressed by that digest the way finished artifacts are.

Works against real AWS S3 or a local S3-compatible endpoint (MinIO) via
CAIRN_S3_ENDPOINT_URL — same trick D1/D2 used to test the claim protocol
against local CockroachDB instead of waiting on cloud credentials.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _cli_exists(bucket: str, key: str) -> bool:
    completed = _run_aws_cli(
        ["s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json"]
    )
    if completed.returncode == 0:
        return True
    detail = f"{completed.stderr}\n{completed.stdout}"
    if any(code in detail for code in _NOT_FOUND_CODES):
        return False
    raise _cli_error("head-object", completed)


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


def _exists(bucket: str, key: str) -> bool:
    if _aws_cli_path() is not None:
        return _cli_exists(bucket, key)
    try:
        get_client().head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        response = getattr(exc, "response", {})
        if response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
            return False
        raise


@dataclass(frozen=True)
class PutResult:
    content_digest: str
    key: str
    s3_uri: str
    already_existed: bool


def put_content_addressed(
    bucket: str, prefix: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> PutResult:
    """Idempotent by construction: the key IS the content hash, so
    uploading identical bytes twice is a HEAD check plus a skip, not a
    redundant PUT — and it's the reuse case Cairn exists for: identical
    computation never pays for identical storage twice, on any machine."""
    digest = content_address(data)
    key = f"{prefix}/{digest}"
    if _exists(bucket, key):
        return PutResult(digest, key, f"s3://{bucket}/{key}", already_existed=True)

    if _aws_cli_path() is not None:
        _cli_put(bucket, key, data, content_type)
    else:
        get_client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return PutResult(digest, key, f"s3://{bucket}/{key}", already_existed=False)


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
    return f"fragments/{work_key}/{fragment_index:04d}.bin"


def put_fragment(bucket: str, work_key: str, fragment_index: int, data: bytes) -> tuple[str, str]:
    """Returns (s3_uri, content_digest); the caller records both in
    run_fragments so a resuming worker can fetch by key and verify by
    digest (PROJECT.md §4.5)."""
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
