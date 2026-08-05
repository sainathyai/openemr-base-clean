"""Prove the production RAG path end-to-end (fastembed + cross-encoder + pgvector).

The retrieval LOGIC is unit-tested with the infra-free stack; this script exists
to confirm the real components integrate: the ONNX embedder and reranker load and
retrieve sensibly, and PgVectorStore round-trips against a live Postgres+pgvector.

Run inside a container that has the deps, networked to a pgvector database:
    DATABASE_URL=postgresql://postgres:pass@ragpg:5432/postgres python scripts/verify_rag.py
"""
import os
import sys

from app.evidence.corpus import load_corpus
from app.evidence.embedding import get_embedder
from app.evidence.rerank import get_reranker
from app.evidence.retriever import HybridRetriever
from app.evidence.vector_store import NumpyVectorStore, PgVectorStore

FAILS = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main():
    print("== A. fastembed embeddings + ONNX cross-encoder reranker ==")
    embedder = get_embedder(prefer_fast=True)
    reranker = get_reranker(prefer_cross_encoder=True)
    print(f"  embedder = {type(embedder).__name__}, reranker = {type(reranker).__name__}")
    check("using FastEmbedEmbedder", type(embedder).__name__ == "FastEmbedEmbedder")
    check("using CrossEncoderReranker", type(reranker).__name__ == "CrossEncoderReranker")

    r = HybridRetriever(embedder=embedder, store=NumpyVectorStore(), reranker=reranker)
    print(f"  embedding dim = {embedder.dim}")

    q1 = "Can this patient stay on metformin with a low eGFR?"
    hits = r.retrieve(q1, k=3)
    print(f"  Q: {q1}")
    for h in hits:
        print(f"     -> {h.id}  ({h.score:.3f})  {h.citation}")
    check("metformin/eGFR query retrieves kdigo-ckd-metformin", "kdigo-ckd-metformin" in [h.id for h in hits])

    q2 = "is ibuprofen safe in chronic kidney disease"
    hits2 = r.retrieve(q2, k=3)
    check("NSAID query retrieves nsaid-ckd-avoid", "nsaid-ckd-avoid" in [h.id for h in hits2])

    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        print("== B. pgvector store round-trip ==")
        corpus = load_corpus()
        vecs = embedder.embed([p.index_text() for p in corpus])
        store = PgVectorStore(dsn=dsn, dim=int(vecs.shape[1]))
        store.ensure_schema()
        store.add([p.id for p in corpus], vecs, [p.index_text() for p in corpus])
        qv = embedder.embed([q1])[0]
        pg_hits = store.search(qv, k=3)
        print(f"  pgvector top-3 for metformin query: {[i for i, _ in pg_hits]}")
        check("pgvector returns rows", len(pg_hits) == 3)
        check("pgvector dense retrieves metformin", "kdigo-ckd-metformin" in [i for i, _ in pg_hits])
        check("pgvector similarity in [0,1]", all(0.0 <= s <= 1.0001 for _, s in pg_hits))
    else:
        print("== B. pgvector skipped (no DATABASE_URL) ==")

    print()
    if FAILS:
        print(f"FAILED: {FAILS}")
        sys.exit(1)
    print("ALL RAG CHECKS PASSED")


if __name__ == "__main__":
    main()
