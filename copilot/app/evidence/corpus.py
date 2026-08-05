"""Guideline corpus loader (Week 2, evidence-retriever worker).

The corpus is a small set of curated clinical-guideline passages, each with
machine-readable source metadata (org, year, section, url) so a retrieved
passage carries a real citation, not just text. This is the "citation contract"
for the RAG side, mirroring the document-span contract on the extraction side.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

_CORPUS_PATH = Path(__file__).resolve().parent.parent / "data" / "guidelines" / "corpus.json"


class GuidelinePassage(BaseModel):
    id: str
    title: str
    org: str
    year: int
    section: str
    url: str
    tags: list[str] = []
    text: str

    @property
    def source_id(self) -> str:
        return f"guideline:{self.id}"

    @property
    def citation(self) -> str:
        """Human-readable citation, e.g. 'KDIGO 2024 — CKD & diabetes management'."""
        return f"{self.org} {self.year} — {self.section}"

    def index_text(self) -> str:
        """Text used for embedding/BM25: title + tags + body (title/tags boost recall)."""
        return f"{self.title}. {' '.join(self.tags)}. {self.text}"


@lru_cache(maxsize=1)
def load_corpus(path: Optional[str] = None) -> list[GuidelinePassage]:
    p = Path(path) if path else _CORPUS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    return [GuidelinePassage(**row) for row in data]


def corpus_by_id() -> dict[str, GuidelinePassage]:
    return {p.id: p for p in load_corpus()}
