"""Amazon Titan Text Embeddings V2 via Bedrock — PLAN.md §2, PROJECT.md §4.1.

1024-dim, `normalize=true`, matching `failure_signatures.embedding
VECTOR(1024)`. Bedrock model access is the one setup step with an approval
queue (PLAN.md §8), and PLAN.md §13's risk table is explicit that "the
demo's correctness path never depends on the LLM": `--no-llm` degrades
embedding-based matching (`strong_semantic`/`weak`) to unavailable, but
`exact` matches, structural classes, and every probe still work without
Bedrock at all. `OfflineFallbackEmbeddingProvider` is that explicit,
clearly-labeled offline path — a deterministic, seeded, locally-computed
vector, used only for local dev and CI when Bedrock access isn't
configured, never for the judged demo path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Protocol

TITAN_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIM = 1024

_WINDOWS_TITAN_PROBE = """\
import json
import sys

request = json.loads(sys.stdin.read())
try:
    import boto3
    client = boto3.client("bedrock-runtime", region_name=request["region"])
    response = client.invoke_model(
        modelId=request["model_id"],
        body=json.dumps(request["body"]),
        contentType="application/json",
        accept="application/json",
    )
    print(json.dumps({"ok": True, "payload": json.loads(response["body"].read())}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
"""


class EmbeddingError(RuntimeError):
    """A Bedrock embedding call failed. The caller decides whether to fall
    back or refuse; this module never silently substitutes one provider
    for another mid-call."""


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class TitanEmbeddingProvider:
    """The production path — real Bedrock, real Titan."""

    def __init__(self, *, region: str | None = None, client: Any | None = None) -> None:
        self._region = region or os.environ.get("CAIRN_AWS_REGION", "us-east-1")
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def embed(self, text: str) -> list[float]:
        # boto3/botocore and psycopg-binary each carry native OpenSSL on
        # Windows. On affected hosts their DLLs conflict hard enough to abort
        # the interpreter before Python can raise an exception. Keep the
        # production Linux/ECS path direct, but isolate the real Bedrock call
        # on Windows so a native child crash becomes a clean EmbeddingError
        # and cannot take the CLI or console server down.
        if sys.platform == "win32" and self._client is None:
            return self._embed_windows_subprocess(text)

        client = self._get_client()
        try:
            response = client.invoke_model(
                modelId=TITAN_MODEL_ID,
                body=json.dumps(
                    {"inputText": text, "dimensions": EMBEDDING_DIM, "normalize": True}
                ),
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:  # botocore.exceptions.ClientError and friends
            raise EmbeddingError(f"Bedrock Titan embedding call failed: {exc}") from exc

        try:
            payload = json.loads(response["body"].read())
            embedding = payload["embedding"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"could not parse Titan embedding response: {exc}") from exc

        return _validated_embedding(embedding)

    def _embed_windows_subprocess(self, text: str) -> list[float]:
        request = {
            "region": self._region,
            "model_id": TITAN_MODEL_ID,
            "body": {"inputText": text, "dimensions": EMBEDDING_DIM, "normalize": True},
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _WINDOWS_TITAN_PROBE],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise EmbeddingError("Bedrock Titan embedding call timed out after 30s") from exc
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            detail = completed.stderr.strip()[:300] or f"child exit code {completed.returncode}"
            raise EmbeddingError(f"Bedrock Titan subprocess failed before returning JSON: {detail}")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise EmbeddingError("Bedrock Titan subprocess returned invalid JSON") from exc
        if not result.get("ok"):
            raise EmbeddingError(f"Bedrock Titan embedding call failed: {result.get('error')}")
        payload = result.get("payload")
        if not isinstance(payload, dict):
            raise EmbeddingError(
                "could not parse Titan embedding response: payload is not an object"
            )
        try:
            embedding = payload["embedding"]
        except KeyError as exc:
            raise EmbeddingError(
                "could not parse Titan embedding response: missing embedding"
            ) from exc
        return _validated_embedding(embedding)


class OfflineFallbackEmbeddingProvider:
    """NOT Titan. A deterministic, seeded, locally-computed unit vector,
    used only when Bedrock access isn't configured (PLAN.md §13's
    `--no-llm` path) — for local dev and CI. Deterministic so the same
    text always produces the same vector run to run (the CI
    vector-fallback-mode integration suite relies on that), and
    unit-normalized so `<=>` cosine distance behaves comparably to a real
    Titan vector. This is a hash-derived vector, not a learned embedding —
    it has no semantic structure and must never be presented as one; its
    only job is to keep the storage/query/tiering machinery exercisable
    without live AWS credentials.
    """

    def embed(self, text: str) -> list[float]:
        return _seeded_unit_vector(text, EMBEDDING_DIM)


def _seeded_unit_vector(text: str, dim: int) -> list[float]:
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for i in range(0, len(block), 4):
            if len(values) >= dim:
                break
            as_int = int.from_bytes(block[i : i + 4], "big")
            values.append((as_int / 0xFFFFFFFF) * 2 - 1)
        counter += 1
    norm = sum(v * v for v in values) ** 0.5
    if norm == 0:
        return values
    return [v / norm for v in values]


def _validated_embedding(embedding: object) -> list[float]:
    if not isinstance(embedding, list) or len(embedding) != EMBEDDING_DIM:
        got = len(embedding) if isinstance(embedding, list) else type(embedding).__name__
        raise EmbeddingError(f"Titan returned dimension {got}, expected {EMBEDDING_DIM}")
    return [float(v) for v in embedding]


def default_provider() -> EmbeddingProvider:
    """Real Bedrock unless explicitly disabled — `CAIRN_NO_LLM` makes the
    fallback a deliberate deployment-time decision, never a silent
    default a demo could ship with by accident."""

    if os.environ.get("CAIRN_NO_LLM"):
        return OfflineFallbackEmbeddingProvider()
    return TitanEmbeddingProvider()
