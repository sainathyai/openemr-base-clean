"""Cross-encoder reranking, swappable (Week 2).

The final stage of hybrid retrieval reorders fused candidates by scoring each
(query, passage) pair jointly, which a bi-encoder cannot do. Production uses an
ONNX cross-encoder via fastembed (`CrossEncoderReranker`). Tests use
`LexicalReranker` (token-overlap), which is deterministic and needs no model, so
the pipeline's ordering contract is verifiable with zero infra.
"""
from __future__ import annotations

from typing import Protocol

from .textutil import content_tokens


def _toks(s: str) -> set[str]:
    return set(content_tokens(s))


class Reranker(Protocol):
    def score(self, query: str, passages: list[str]) -> list[float]:
        """Return a relevance score per passage (higher = more relevant)."""
        ...


class LexicalReranker:
    """Deterministic token-overlap reranker (Jaccard). Test/offline default."""

    def score(self, query: str, passages: list[str]) -> list[float]:
        q = _toks(query)
        out = []
        for p in passages:
            pt = _toks(p)
            inter = len(q & pt)
            union = len(q | pt) or 1
            out.append(inter / union)
        return out


class CrossEncoderReranker:
    """ONNX cross-encoder via fastembed (lazy import; production path)."""

    def __init__(self, model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy

        self._model = TextCrossEncoder(model_name=model_name)
        self.model_name = model_name

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, passages)]


def cross_encoder_available() -> bool:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401

        return True
    except Exception:
        return False


def get_reranker(prefer_cross_encoder: bool = True) -> Reranker:
    if prefer_cross_encoder and cross_encoder_available():
        return CrossEncoderReranker()
    return LexicalReranker()
