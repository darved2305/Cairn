"""Preflight degradation: embedding/DB failure must not authorize or crash."""

from __future__ import annotations

from cairn.embeddings import EmbeddingError
from cairn.flight.preflight import preflight_structured_memory


class _BoomPool:
    pass


def test_preflight_embedding_error_fails_open(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _boom(_text: str) -> list[float]:
        raise EmbeddingError("NOT_AUTHORIZED")

    class _Prov:
        def embed(self, text: str) -> list[float]:
            return _boom(text)

    monkeypatch.setattr("cairn.flight.preflight.default_provider", lambda: _Prov())
    outcome = preflight_structured_memory(
        _BoomPool(),  # type: ignore[arg-type]
        stage="checkpoint",
        error_class_hint="ValueError",
        structured={"input_dim": 384, "embedding_dim": 768},
        summary_text="shape mismatch",
    )
    assert outcome.blocked is False
    assert "embedding unavailable" in outcome.reason
