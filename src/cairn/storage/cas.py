"""Conditional immutable blob publication under ``cas/sha256/<digest>``.

S3 keys addressed by content digest are only safe if a second writer cannot
replace the bytes under that digest. ``If-None-Match: *`` makes first-writer
wins atomic; a 412 then forces an exact-version fetch and rehash so a
bucket-integrity failure cannot be mistaken for a benign collision.

``BlobUnavailable`` (timeout/5xx) must never mark a blob INVALID.
``BlobIntegrityError`` (digest mismatch) is the only path that may.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from cairn.storage import s3

Digest = str


class BlobUnavailable(RuntimeError):
    """Transient object-store failure. Must not quarantine global integrity."""


class BlobIntegrityError(RuntimeError):
    """Stored bytes do not match the content-addressed key. Quarantine-worthy."""


@dataclass(frozen=True, slots=True)
class ContentBlobRef:
    blob_digest: Digest
    bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class BlobPublication:
    blob_digest: Digest
    bucket: str
    object_key: str
    version_id: str
    checksum_sha256: str
    size_bytes: int
    already_existed: bool

    def __post_init__(self) -> None:
        if len(self.blob_digest) != 64 or any(
            c not in "0123456789abcdef" for c in self.blob_digest
        ):
            raise ValueError(
                f"blob_digest must be 64 lowercase hex chars, got {self.blob_digest!r}"
            )
        if not self.object_key.startswith("cas/sha256/"):
            raise ValueError(f"CAS object_key must be under cas/sha256/, got {self.object_key!r}")
        if not self.version_id:
            raise ValueError("version_id is required; CAS publication needs a versioned bucket")


def cas_object_key(digest: Digest) -> str:
    return f"cas/sha256/{digest}"


def _sha256_hex(data: bytes) -> Digest:
    return hashlib.sha256(data).hexdigest()


def _checksum_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _is_precondition_failed(exc: BaseException) -> bool:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in {"PreconditionFailed", "412"}
    detail = str(exc)
    return "PreconditionFailed" in detail or "412" in detail


def _is_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (EndpointConnectionError, TimeoutError, BotoCoreError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        http = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if http >= 500 or code in {"ServiceUnavailable", "SlowDown", "RequestTimeout"}:
            return True
    return False


def _head_version(bucket: str, key: str) -> str:
    """Return the current VersionId for an existing object."""
    try:
        if s3._aws_cli_path() is not None:
            completed = s3._run_aws_cli(
                [
                    "s3api",
                    "head-object",
                    "--bucket",
                    bucket,
                    "--key",
                    key,
                    "--output",
                    "json",
                ]
            )
            if completed.returncode != 0:
                raise s3._cli_error("head-object", completed)
            import json

            payload = json.loads(completed.stdout)
            version_id = payload.get("VersionId")
        else:
            resp = s3.get_client().head_object(Bucket=bucket, Key=key)
            version_id = resp.get("VersionId")
    except Exception as exc:
        if _is_unavailable(exc):
            raise BlobUnavailable(
                f"head_object unavailable for s3://{bucket}/{key}: {exc}"
            ) from exc
        raise
    if not version_id or version_id == "null":
        raise BlobIntegrityError(
            f"s3://{bucket}/{key} has no VersionId; CAS requires a versioned bucket"
        )
    return str(version_id)


def _get_versioned(bucket: str, key: str, version_id: str) -> bytes:
    try:
        if s3._aws_cli_path() is not None:
            import tempfile
            from pathlib import Path

            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix="cairn-cas-get-", suffix=".bin", delete=False
                ) as f:
                    temp_path = Path(f.name)
                completed = s3._run_aws_cli(
                    [
                        "s3api",
                        "get-object",
                        "--bucket",
                        bucket,
                        "--key",
                        key,
                        "--version-id",
                        version_id,
                        str(temp_path),
                        "--output",
                        "json",
                    ]
                )
                if completed.returncode != 0:
                    raise s3._cli_error("get-object", completed)
                return temp_path.read_bytes()
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
        resp = s3.get_client().get_object(Bucket=bucket, Key=key, VersionId=version_id)
        body: bytes = resp["Body"].read()
        return body
    except Exception as exc:
        if _is_unavailable(exc):
            raise BlobUnavailable(
                f"get_object unavailable for s3://{bucket}/{key}?versionId={version_id}: {exc}"
            ) from exc
        raise


def _put_if_none_match(bucket: str, key: str, data: bytes, *, content_type: str) -> str:
    """Conditional PUT. Returns VersionId. Raises ClientError/CLI error on 412."""
    checksum = _checksum_b64(data)
    if s3._aws_cli_path() is not None:
        import json
        import tempfile
        from pathlib import Path

        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="cairn-cas-put-", suffix=".bin", delete=False
            ) as f:
                f.write(data)
                temp_path = Path(f.name)
            completed = s3._run_aws_cli(
                [
                    "s3api",
                    "put-object",
                    "--bucket",
                    bucket,
                    "--key",
                    key,
                    "--body",
                    str(temp_path),
                    "--content-type",
                    content_type,
                    "--if-none-match",
                    "*",
                    "--checksum-sha256",
                    checksum,
                    "--checksum-algorithm",
                    "SHA256",
                    "--output",
                    "json",
                ]
            )
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
                raise s3._cli_error("put-object", completed)
            payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
            version_id = payload.get("VersionId")
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    else:
        resp = s3.get_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            IfNoneMatch="*",
            ChecksumSHA256=checksum,
            ChecksumAlgorithm="SHA256",
        )
        version_id = resp.get("VersionId")
    if not version_id or version_id == "null":
        raise BlobIntegrityError(
            f"put_object for s3://{bucket}/{key} returned no VersionId; "
            "enable bucket versioning before CAS publication"
        )
    return str(version_id)


def _publication_from_existing(
    bucket: str, key: str, expected_digest: Digest, data: bytes
) -> BlobPublication:
    """After 412: fetch the exact current version, rehash, accept or raise."""
    version_id = _head_version(bucket, key)
    stored = _get_versioned(bucket, key, version_id)
    actual = _sha256_hex(stored)
    if actual != expected_digest:
        raise BlobIntegrityError(
            f"412 at s3://{bucket}/{key} but stored bytes hash to {actual}, "
            f"expected {expected_digest} — bucket integrity failure, not a collision"
        )
    return BlobPublication(
        blob_digest=expected_digest,
        bucket=bucket,
        object_key=key,
        version_id=version_id,
        checksum_sha256=_checksum_b64(stored),
        size_bytes=len(stored),
        already_existed=True,
    )


def publish_blob(
    bucket: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> BlobPublication:
    """PUT with If-None-Match: * and ChecksumSHA256 under ``cas/sha256/<digest>``.

    On 412 PreconditionFailed: fetch the exact current VersionId, rehash the
    downloaded bytes, and return that publication with already_existed=True.
    A 412 whose stored bytes hash to a different digest is a bucket-integrity
    failure, not a benign collision.
    """
    digest = _sha256_hex(data)
    key = cas_object_key(digest)
    try:
        version_id = _put_if_none_match(bucket, key, data, content_type=content_type)
    except Exception as exc:
        if _is_precondition_failed(exc):
            return _publication_from_existing(bucket, key, digest, data)
        if _is_unavailable(exc):
            raise BlobUnavailable(
                f"publish_blob unavailable for s3://{bucket}/{key}: {exc}"
            ) from exc
        raise
    return BlobPublication(
        blob_digest=digest,
        bucket=bucket,
        object_key=key,
        version_id=version_id,
        checksum_sha256=_checksum_b64(data),
        size_bytes=len(data),
        already_existed=False,
    )


def fetch_blob_verified(ref: ContentBlobRef) -> bytes:
    """GetObject at the exact VersionId, rehash locally, raise on mismatch.

    A timeout or 5xx raises BlobUnavailable, which is explicitly NOT corruption.
    """
    try:
        data = _get_versioned(ref.bucket, ref.object_key, ref.version_id)
    except BlobUnavailable:
        raise
    except Exception as exc:
        if _is_unavailable(exc):
            raise BlobUnavailable(
                f"fetch_blob_verified unavailable for "
                f"s3://{ref.bucket}/{ref.object_key}?versionId={ref.version_id}: {exc}"
            ) from exc
        raise
    actual = _sha256_hex(data)
    if actual != ref.blob_digest:
        raise BlobIntegrityError(
            f"blob {ref.blob_digest} at version {ref.version_id} rehashed to {actual}"
        )
    if len(data) != ref.size_bytes:
        raise BlobIntegrityError(
            f"blob {ref.blob_digest} size mismatch: expected {ref.size_bytes}, got {len(data)}"
        )
    return data
