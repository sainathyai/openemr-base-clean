"""Slice 3: hybrid retrieval (BM25 + dense + RRF + rerank) over the guideline corpus.

Uses the infra-free stack (HashingEmbedder + NumpyVectorStore + LexicalReranker)
so the fusion and citation contract are verified with no DB and no model download.
The fastembed/pgvector production path is verified separately in a container.
"""
from app.evidence.corpus import load_corpus
from app.evidence.embedding import HashingEmbedder
from app.evidence.rerank import LexicalReranker
from app.evidence.retriever import HybridRetriever, _rrf
from app.evidence.vector_store import NumpyVectorStore


def _retriever():
    return HybridRetriever(
        embedder=HashingEmbedder(),
        store=NumpyVectorStore(),
        reranker=LexicalReranker(),
    )


def test_corpus_loads_with_citations():
    corpus = load_corpus()
    assert len(corpus) >= 15
    p = corpus[0]
    assert p.source_id.startswith("guideline:")
    assert p.citation and p.url


def test_rrf_rewards_agreement_across_lists():
    # id "b" is ranked high by both lists -> should top the fused order
    fused = _rrf([["a", "b", "c"], ["b", "d", "a"]])
    top = max(fused, key=lambda i: fused[i])
    assert top == "b"


def test_metformin_egfr_query_retrieves_the_right_guideline():
    r = _retriever()
    hits = r.retrieve("Can this patient stay on metformin with a low eGFR?", k=3)
    ids = [h.id for h in hits]
    assert "kdigo-ckd-metformin" in ids
    top = hits[0]
    assert top.source_id == f"guideline:{top.id}"
    assert top.citation and top.url  # citation contract satisfied


def test_nsaid_ckd_query_flags_avoidance_guideline():
    r = _retriever()
    hits = r.retrieve("is ibuprofen safe in chronic kidney disease", k=3)
    assert "nsaid-ckd-avoid" in [h.id for h in hits]


def test_retrieve_reports_hybrid_provenance():
    r = _retriever()
    hits = r.retrieve("potassium monitoring on an ACE inhibitor in CKD", k=4)
    assert hits
    # at least one hit was found by BOTH arms (dense and sparse rank present)
    assert any(h.dense_rank is not None and h.sparse_rank is not None for h in hits)


def test_k_bounds_result_count():
    r = _retriever()
    assert len(r.retrieve("CKD staging by eGFR", k=2)) <= 2
