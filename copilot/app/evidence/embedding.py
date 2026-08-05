"""Text embedding, swappable (Week 2).

Production uses `FastEmbedEmbedder` (a quantized ONNX model via fastembed: CPU,
torch-free, a few hundred MB, so it fits the small deploy box). Tests and the
fast local loop use `HashingEmbedder` (deterministic hashed n-grams, no model
download, no network) so the retrieval plumbing is verifiable with zero infra.

Both return L2-normalized row vectors, so cosine similarity is a dot product.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional, Protocol

import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dim) float32 L2-normalized matrix."""
        ...


class HashingEmbedder:
    """Deterministic hashed unigram+bigram embedder. No download, no network.

    Not semantically rich, but stable and dependency-light: enough to exercise
    dense retrieval, RRF fusion, and the vector-store contract in tests.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = _tokens(text)
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        return v

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalize(np.vstack([self._vec(t) for t in texts]))


class FastEmbedEmbedder:
    """ONNX embedder via fastembed (lazy import so tests never need it)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding  # lazy

        self._model = TextEmbedding(model_name=model_name)
        self.model_name = model_name
        # bge-small is 384-dim; confirm from a probe on first embed.
        self.dim = 384

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        mat = np.array(list(self._model.embed(texts)), dtype=np.float32)
        self.dim = mat.shape[1]
        return _l2_normalize(mat)


def fastembed_available() -> bool:
    try:
        import fastembed  # noqa: F401

        return True
    except Exception:
        return False


def get_embedder(prefer_fast: bool = True, model_name: Optional[str] = None) -> Embedder:
    """Pick the best available embedder. Falls back to hashing off-box/in-tests."""
    if prefer_fast and fastembed_available():
        return FastEmbedEmbedder(model_name or "BAAI/bge-small-en-v1.5")
    return HashingEmbedder()
