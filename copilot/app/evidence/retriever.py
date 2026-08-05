"""Hybrid retrieval over the guideline corpus (Week 2, evidence-retriever worker).

Pipeline: sparse (BM25) + dense (embedding/vector-store) candidate lists, fused
by Reciprocal Rank Fusion, then a cross-encoder reorders the fused shortlist.
RRF is used instead of score addition because BM25 and cosine scores are on
different scales; rank fusion is scale-free and robust.

Every returned passage carries its guideline citation (source_id + org/year/
section/url), satisfying the Week-2 citation contract on the RAG side.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from .corpus import GuidelinePassage, load_corpus
from .embedding import Embedder, get_embedder
from .rerank import Reranker, get_reranker
from .textutil import content_tokens as _toks
from .vector_store import NumpyVectorStore, VectorStore

_RRF_K = 60  # standard RRF damping constant


class RetrievedPassage(BaseModel):
    id: str
    source_id: str
    title: str
    citation: str
    url: str
    text: str
    score: float          # final reranker score
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None


def _rrf(rank_lists: list[list[str]]) -> dict[str, float]:
    """Reciprocal Rank Fusion over several ranked id lists."""
    fused: dict[str, float] = {}
    for ranks in rank_lists:
        for rank, id_ in enumerate(ranks):
            fused[id_] = fused.get(id_, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return fused


class HybridRetriever:
    """Sparse + dense + RRF + cross-encoder rerank over guideline passages."""

    def __init__(
        self,
        passages: Optional[list[GuidelinePassage]] = None,
        embedder: Optional[Embedder] = None,
        store: Optional[VectorStore] = None,
        reranker: Optional[Reranker] = None,
    ):
        self.passages = passages if passages is not None else load_corpus()
        self._by_id = {p.id: p for p in self.passages}
        self.embedder = embedder or get_embedder()
        self.reranker = reranker or get_reranker()

        index_texts = [p.index_text() for p in self.passages]
        # sparse
        self._bm25 = BM25Okapi([_toks(t) for t in index_texts])
        # dense
        self.store = store or NumpyVectorStore()
        vectors = self.embedder.embed(index_texts)
        self.store.add([p.id for p in self.passages], vectors, index_texts)

    def _dense_ranks(self, query: str, k: int) -> list[str]:
        qv = self.embedder.embed([query])
        hits = self.store.search(qv[0], k)
        return [id_ for id_, _ in hits]

    def _sparse_ranks(self, query: str, k: int) -> list[str]:
        scores = self._bm25.get_scores(_toks(query))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        ids = [self.passages[i].id for i in order if scores[i] > 0][:k]
        return ids

    def retrieve(
        self, query: str, k: int = 4, candidate_k: int = 8
    ) -> list[RetrievedPassage]:
        dense = self._dense_ranks(query, candidate_k)
        sparse = self._sparse_ranks(query, candidate_k)
        fused = _rrf([dense, sparse])
        if not fused:
            return []
        shortlist = sorted(fused, key=lambda i: -fused[i])[:candidate_k]

        # cross-encoder rerank the fused shortlist
        texts = [self._by_id[i].text for i in shortlist]
        rr = self.reranker.score(query, texts)
        ranked = sorted(zip(shortlist, rr), key=lambda t: -t[1])[:k]

        dense_pos = {id_: r for r, id_ in enumerate(dense)}
        sparse_pos = {id_: r for r, id_ in enumerate(sparse)}
        out: list[RetrievedPassage] = []
        for id_, score in ranked:
            p = self._by_id[id_]
            out.append(
                RetrievedPassage(
                    id=p.id,
                    source_id=p.source_id,
                    title=p.title,
                    citation=p.citation,
                    url=p.url,
                    text=p.text,
                    score=float(score),
                    dense_rank=dense_pos.get(id_),
                    sparse_rank=sparse_pos.get(id_),
                )
            )
        return out
