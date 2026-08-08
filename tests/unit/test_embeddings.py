from __future__ import annotations

import json
import math
import subprocess
from io import BytesIO
from typing import Any

import pytest

import cairn.embeddings as embeddings_module
from cairn.embeddings import (
    EMBEDDING_DIM,
    EmbeddingError,
    OfflineFallbackEmbeddingProvider,
    TitanEmbeddingProvider,
    default_provider,
)

# ---------------------------------------------------------------------------
# OfflineFallbackEmbeddingProvider
# ---------------------------------------------------------------------------


def test_offline_provider_returns_correct_dimension() -> None:
    vector = OfflineFallbackEmbeddingProvider().embed("hello world")
    assert len(vector) == EMBEDDING_DIM


def test_offline_provider_is_deterministic() -> None:
    provider = OfflineFallbackEmbeddingProvider()
    assert provider.embed("same text") == provider.embed("same text")


def test_offline_provider_differs_across_texts() -> None:
    provider = OfflineFallbackEmbeddingProvider()
    assert provider.embed("text a") != provider.embed("text b")


def test_offline_provider_is_unit_normalized() -> None:
    vector = OfflineFallbackEmbeddingProvider().embed("normalize me")
    magnitude = math.sqrt(sum(v * v for v in vector))
    assert magnitude == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# TitanEmbeddingProvider — request/response handling, no real AWS call
# ---------------------------------------------------------------------------


class _FakeBody:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._buf = BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()


class _FakeBedrockClient:
    def __init__(
        self, response_payload: dict[str, Any] | None = None, *, raise_error: bool = False
    ):
        self._response_payload = response_payload
        self._raise_error = raise_error
        self.last_request: dict[str, Any] | None = None

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        if self._raise_error:
            raise RuntimeError("simulated ClientError: AccessDeniedException")
        self.last_request = kwargs
        assert self._response_payload is not None
        return {"body": _FakeBody(self._response_payload)}


def test_titan_provider_sends_correct_request_shape() -> None:
    client = _FakeBedrockClient({"embedding": [0.1] * EMBEDDING_DIM, "inputTextTokenCount": 3})
    provider = TitanEmbeddingProvider(client=client)
    provider.embed("mpnet needs input_dim=768")

    assert client.last_request is not None
    body = json.loads(client.last_request["body"])
    assert body == {
        "inputText": "mpnet needs input_dim=768",
        "dimensions": EMBEDDING_DIM,
        "normalize": True,
    }
    assert client.last_request["modelId"] == "amazon.titan-embed-text-v2:0"


def test_titan_provider_parses_response_embedding() -> None:
    expected = [float(i) / 1000 for i in range(EMBEDDING_DIM)]
    client = _FakeBedrockClient({"embedding": expected})
    provider = TitanEmbeddingProvider(client=client)
    result = provider.embed("some failure text")
    assert result == expected


def test_titan_provider_wraps_client_errors() -> None:
    client = _FakeBedrockClient(raise_error=True)
    provider = TitanEmbeddingProvider(client=client)
    with pytest.raises(EmbeddingError, match="Bedrock Titan embedding call failed"):
        provider.embed("text")


def test_windows_titan_native_failure_becomes_embedding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embeddings_module.sys, "platform", "win32")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["python"],
            3221226505,
            stdout="",
            stderr="OPENSSL_Uplink: no OPENSSL_Applink",
        )

    monkeypatch.setattr(embeddings_module.subprocess, "run", fake_run)

    with pytest.raises(EmbeddingError, match="subprocess failed.*OPENSSL_Uplink"):
        TitanEmbeddingProvider().embed("text")


def test_titan_provider_rejects_wrong_dimension_response() -> None:
    client = _FakeBedrockClient({"embedding": [0.1, 0.2, 0.3]})
    provider = TitanEmbeddingProvider(client=client)
    with pytest.raises(EmbeddingError, match="expected 1024"):
        provider.embed("text")


def test_titan_provider_rejects_malformed_response() -> None:
    client = _FakeBedrockClient({"not_embedding": []})
    provider = TitanEmbeddingProvider(client=client)
    with pytest.raises(EmbeddingError, match="could not parse"):
        provider.embed("text")


# ---------------------------------------------------------------------------
# default_provider
# ---------------------------------------------------------------------------


def test_default_provider_is_titan_unless_no_llm_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAIRN_NO_LLM", raising=False)
    assert isinstance(default_provider(), TitanEmbeddingProvider)


def test_default_provider_falls_back_when_no_llm_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_NO_LLM", "1")
    assert isinstance(default_provider(), OfflineFallbackEmbeddingProvider)
