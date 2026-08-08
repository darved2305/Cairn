from __future__ import annotations

import pytest

from cairn.agent import loop


def _artifact() -> loop.ArtifactHandle:
    return loop.ArtifactHandle(
        artifact_id="a" * 64,
        s3_uri=f"s3://cairn-demo/checkpoint/{'a' * 64}",
        stage="checkpoint",
        work_key="b" * 64,
        env_fingerprint="c" * 64,
    )


def test_upstream_read_failure_carries_exact_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact()

    def fail(_bucket: str, _key: str) -> bytes:
        raise FileNotFoundError("real key is absent")

    monkeypatch.setattr(loop.s3, "get_bytes", fail)

    with pytest.raises(loop.UpstreamArtifactUnavailable) as raised:
        loop._get_upstream_bytes("cairn-demo", artifact)  # noqa: SLF001

    assert raised.value.artifact_id == artifact.artifact_id
    assert artifact.s3_uri in str(raised.value)
    assert isinstance(raised.value.__cause__, FileNotFoundError)


def test_upstream_read_uses_uri_key(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact()
    observed: list[tuple[str, str]] = []

    def read(bucket: str, key: str) -> bytes:
        observed.append((bucket, key))
        return b"real object bytes"

    monkeypatch.setattr(loop.s3, "get_bytes", read)

    assert loop._get_upstream_bytes("cairn-demo", artifact) == b"real object bytes"  # noqa: SLF001
    assert observed == [("cairn-demo", f"checkpoint/{'a' * 64}")]
