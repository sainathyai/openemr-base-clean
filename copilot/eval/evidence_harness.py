"""Golden-eval harness for the Week-2 evidence agent.

The systematic complement to the evidence unit tests: a corpus of hand-authored
cases that asserts the two graded behaviours hold across many adversarial
phrasings, and must go RED if either is weakened (that is the point — the eval
catches injected regressions).

Two suites:
  - ExtractionCase : the extraction gate. A (possibly fabricated/misread) value
                     is run through verify_*_extraction over the real sample
                     document; the rubric asserts kept-vs-dropped and, for drops,
                     the reason. Weakening the gate flips a dropped case to kept.
  - RetrievalCase  : hybrid RAG relevance. A clinical query must surface the
                     expected guideline in top-k, with a real citation. Breaking
                     fusion/ranking drops the expected id out of top-k.

Stub-only and deterministic (HashingEmbedder + LexicalReranker + StubExtractor):
no LLM, no DB, no model download, so it runs in CI on every change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Optional

from app.evidence.embedding import HashingEmbedder
from app.evidence.extract import verify_intake_extraction, verify_lab_extraction
from app.evidence.pdf_parse import parse_pdf
from app.evidence.rerank import LexicalReranker
from app.evidence.retriever import HybridRetriever
from app.evidence.sample_docs import intake_form_pdf, lab_report_pdf
from app.evidence.schemas import (
    BBox,
    DocumentSpan,
    ExtractedLab,
    ExtractedStatement,
    IntakeFormExtraction,
    LabReportExtraction,
)
from app.evidence.vector_store import NumpyVectorStore

_DUMMY_BOX = BBox(page=0, x0=0, y0=0, x1=1, y1=1)


# ---------- rubric primitives ----------
@dataclass
class Rubric:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseReport:
    case_id: str
    suite: str
    intent: str
    rubrics: list[Rubric]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.rubrics)


# ---------- shared, cached fixtures ----------
@lru_cache(maxsize=1)
def _lab_doc():
    return parse_pdf(lab_report_pdf(), doc_id="lab-001", doc_type="lab_report")


@lru_cache(maxsize=1)
def _intake_doc():
    return parse_pdf(intake_form_pdf(), doc_id="intake-7", doc_type="intake_form")


@lru_cache(maxsize=1)
def _retriever() -> HybridRetriever:
    return HybridRetriever(
        embedder=HashingEmbedder(), store=NumpyVectorStore(), reranker=LexicalReranker()
    )


# ---------- extraction cases ----------
@dataclass
class ExtractionCase:
    """One extracted fact run through the gate. `span_phrase` is the text the
    extractor claims to have located; the gate re-locates it in the real document.
    Set a value the document does not carry, or a phrase it does not contain, to
    simulate a misread/hallucination that the gate must drop."""

    id: str
    intent: str
    kind: Literal["lab", "statement"]
    span_phrase: str
    expect_kept: bool
    value: Optional[str] = None
    unit: Optional[str] = None
    analyte: Optional[str] = None
    stmt_kind: Literal["medication", "allergy", "problem"] = "medication"
    expect_reason: Optional[str] = None

    def evaluate(self) -> CaseReport:
        if self.kind == "lab":
            doc = _lab_doc()
            span = DocumentSpan(doc_id=doc.doc_id, doc_type="lab_report", page=0,
                                text=self.span_phrase, bbox=_DUMMY_BOX)
            ext = LabReportExtraction(doc_id=doc.doc_id, labs=[ExtractedLab(
                analyte=self.analyte or "Analyte", value=self.value,
                unit=self.unit, span=span)])
            kept, dropped = verify_lab_extraction(doc, ext)
            got_kept = len(kept.labs) == 1
        else:
            doc = _intake_doc()
            span = DocumentSpan(doc_id=doc.doc_id, doc_type="intake_form", page=0,
                                text=self.span_phrase, bbox=_DUMMY_BOX)
            ext = IntakeFormExtraction(doc_id=doc.doc_id, statements=[ExtractedStatement(
                kind=self.stmt_kind, text=self.span_phrase, span=span)])
            kept, dropped = verify_intake_extraction(doc, ext)
            got_kept = len(kept.statements) == 1

        reasons = " ; ".join(d["reason"] for d in dropped)
        rubrics = [Rubric(
            "verdict", got_kept == self.expect_kept,
            f"kept={got_kept} expected {self.expect_kept}; reasons=[{reasons}]")]
        if not self.expect_kept and self.expect_reason:
            rubrics.append(Rubric(
                "drop_reason", self.expect_reason.lower() in reasons.lower(),
                f"expected '{self.expect_reason}' within [{reasons}]"))
        return CaseReport(self.id, "extraction", self.intent, rubrics)


# ---------- retrieval cases ----------
@dataclass
class RetrievalCase:
    id: str
    intent: str
    query: str
    expect_id: str
    k: int = 4
    forbid_id: Optional[str] = None  # this id must NOT appear in top-k

    def evaluate(self) -> CaseReport:
        hits = _retriever().retrieve(self.query, k=self.k)
        ids = [h.id for h in hits]
        rubrics = [Rubric(
            "expected_in_topk", self.expect_id in ids,
            f"{self.expect_id} not in top-{self.k}: {ids}")]
        # every hit must carry a real citation (the RAG citation contract)
        rubrics.append(Rubric(
            "citations_present", all(h.citation and h.url for h in hits),
            "a retrieved passage was missing citation/url"))
        if self.forbid_id is not None:
            rubrics.append(Rubric(
                "forbidden_absent", self.forbid_id not in ids,
                f"forbidden {self.forbid_id} appeared in {ids}"))
        return CaseReport(self.id, "retrieval", self.intent, rubrics)


# ---------- scoring ----------
@dataclass
class Scorecard:
    reports: list[CaseReport]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.reports)

    def _suite(self, name: str) -> list[CaseReport]:
        return [r for r in self.reports if r.suite == name]

    def rate(self, suite: str) -> tuple[int, int]:
        rs = self._suite(suite)
        return sum(r.passed for r in rs), len(rs)

    def overall(self) -> tuple[int, int]:
        return sum(r.passed for r in self.reports), len(self.reports)

    def failures(self) -> list[CaseReport]:
        return [r for r in self.reports if not r.passed]


def run_all() -> Scorecard:
    from .evidence_cases import EXTRACTION_CASES, RETRIEVAL_CASES

    reports = [c.evaluate() for c in EXTRACTION_CASES]
    reports += [c.evaluate() for c in RETRIEVAL_CASES]
    return Scorecard(reports)


def format_scorecard(sc: Scorecard) -> str:
    lines = ["Evidence agent golden eval", "=" * 34]
    for suite, label in (("extraction", "Extraction gate (grounded-or-dropped)"),
                         ("retrieval", "Hybrid RAG relevance (cited)")):
        passed, total = sc.rate(suite)
        lines.append(f"\n{label}: {passed}/{total} cases")
        for r in sc._suite(suite):
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"  [{mark}] {r.case_id}: {r.intent}")
            if not r.passed:
                for rub in r.rubrics:
                    if not rub.passed:
                        lines.append(f"          - {rub.name}: {rub.detail}")
    p, t = sc.overall()
    lines.append(f"\nTOTAL: {p}/{t}")
    return "\n".join(lines)
