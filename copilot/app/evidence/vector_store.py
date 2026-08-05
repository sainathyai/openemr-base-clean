"""Vector store, swappable (Week 2).

`NumpyVectorStore` keeps the index in memory (cosine over a matrix) so tests and
the fast local loop need no database. `PgVectorStore` is the production backend:
Postgres + pgvector, indexed offline at build time so serving only embeds the
short query. Retrieval code depends on the `VectorStore` protocol, not either
implementation, mirroring the FixtureClient/FhirClient split already in the app.
"""
from __future__ import annotations

from typing import Optional, Protocol

import numpy as np


class VectorStore(Protocol):
    def add(self, ids: list[str], vectors: np.ndarray, texts: list[str]) -> None: ...

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]:
        """Return up to k (id, cosine_similarity) pairs, highest first."""
        ...


class NumpyVectorStore:
    """In-memory cosine store. Vectors are assumed L2-normalized (see embedding)."""

    def __init__(self):
        self._ids: list[str] = []
        self._mat: Optional[np.ndarray] = None

    def add(self, ids: list[str], vectors: np.ndarray, texts: list[str]) -> None:
        if len(ids) == 0:
            return
        self._ids.extend(ids)
        self._mat = vectors if self._mat is None else np.vstack([self._mat, vectors])

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]:
        if self._mat is None or not self._ids:
            return []
        q = query.reshape(-1)
        sims = self._mat @ q  # cosine, since both sides are normalized
        k = min(k, len(self._ids))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(self._ids[i], float(sims[i])) for i in top]


class PgVectorStore:
    """Postgres + pgvector backend. psycopg imported lazily (prod-only path)."""

    def __init__(self, dsn: str, dim: int, table: str = "guideline_chunks"):
        self.dsn = dsn
        self.dim = dim
        self.table = table

    def _raw_connect(self):
        import psycopg  # lazy

        return psycopg.connect(self.dsn)

    def _connect(self):
        # register_vector requires the `vector` type to already exist, so callers
        # must have run ensure_schema() first.
        from pgvector.psycopg import register_vector

        conn = self._raw_connect()
        register_vector(conn)
        return conn

    def ensure_schema(self) -> None:
        # Create the extension on a raw connection BEFORE anything tries to
        # register the vector type adapter (chicken-and-egg otherwise).
        with self._raw_connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self.table} ("
                f"  id text PRIMARY KEY,"
                f"  text text NOT NULL,"
                f"  embedding vector({self.dim})"
                f")"
            )
            # cosine index; ivfflat needs data first, so build lazily elsewhere.
            conn.commit()

    def add(self, ids: list[str], vectors: np.ndarray, texts: list[str]) -> None:
        if len(ids) == 0:
            return
        with self._connect() as conn, conn.cursor() as cur:
            for id_, vec, text in zip(ids, vectors, texts):
                cur.execute(
                    f"INSERT INTO {self.table} (id, text, embedding) VALUES (%s, %s, %s) "
                    f"ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                    f"embedding = EXCLUDED.embedding",
                    (id_, text, np.asarray(vec, dtype=np.float32)),
                )
            conn.commit()

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]:
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        with self._connect() as conn, conn.cursor() as cur:
            # 1 - cosine_distance = cosine similarity (pgvector <=> is distance)
            cur.execute(
                f"SELECT id, 1 - (embedding <=> %s) AS sim FROM {self.table} "
                f"ORDER BY embedding <=> %s LIMIT %s",
                (q, q, k),
            )
            return [(row[0], float(row[1])) for row in cur.fetchall()]
