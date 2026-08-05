"""Slice 4: extractor + extraction gate + supervisor/worker graph.

Runs entirely on the infra-free stack (StubExtractor + HashingEmbedder +
LexicalReranker), so no LLM, no DB, no model download.
"""
import asyncio

from app.evidence import agent as evidence_agent
from app.evidence.embedding import HashingEmbedder
from app.evidence.extract import (
    StubExtractor,
    verify_intake_extraction,
    verify_lab_extraction,
)
from app.evidence.pdf_parse import parse_pdf
from app.evidence.rerank import LexicalReranker
from app.evidence.retriever import HybridRetriever
from app.evidence.sample_docs import intake_form_pdf, lab_report_pdf
from app.evidence.schemas import BBox, DocumentSpan, ExtractedLab, is_document_source
from app.evidence.vector_store import NumpyVectorStore


def _lab_doc():
    return parse_pdf(lab_report_pdf(), doc_id="lab-001", doc_type="lab_report")


def _intake_doc():
    return parse_pdf(intake_form_pdf(), doc_id="intake-7", doc_type="intake_form")


def _retriever():
    return HybridRetriever(
        embedder=HashingEmbedder(), store=NumpyVectorStore(), reranker=LexicalReranker()
    )


# ---- extractor -----------------------------------------------------------
def test_stub_extracts_labs_with_values_and_spans():
    ext = StubExtractor().extract(_lab_doc())
    by = {l.analyte.lower(): l for l in ext.labs}
    assert "creatinine" in by and by["creatinine"].value == "2.10"
    assert by["creatinine"].unit == "mg/dL"
    assert "egfr" in by and by["egfr"].value == "38"
    # each lab carries a resolvable document span
    assert all(is_document_source(l.span.source_id) for l in ext.labs)


def test_stub_extracts_intake_statements_by_section():
    ext = StubExtractor().extract(_intake_doc())
    kinds = {s.kind for s in ext.statements}
    assert {"medication", "allergy", "problem"} <= kinds
    texts = " ".join(s.text.lower() for s in ext.statements)
    assert "lisinopril" in texts and "penicillin" in texts


# ---- extraction gate -----------------------------------------------------
def test_gate_keeps_grounded_labs():
    doc = _lab_doc()
    kept, dropped = verify_lab_extraction(doc, StubExtractor().extract(doc))
    assert kept.labs and not dropped


def test_gate_drops_hallucinated_value():
    doc = _lab_doc()
    ext = StubExtractor().extract(doc)
    # inject a fabricated troponin the document never reported
    ext.labs.append(
        ExtractedLab(
            analyte="Troponin",
            value="9.99",
            unit="ng/mL",
            span=DocumentSpan(
                doc_id="lab-001", doc_type="lab_report", page=0,
                text="Troponin 9.99", bbox=BBox(page=0, x0=0, y0=0, x1=10, y1=10),
            ),
        )
    )
    kept, dropped = verify_lab_extraction(doc, ext)
    assert any(d["analyte"] == "Troponin" for d in dropped)
    assert "Troponin" not in [l.analyte for l in kept.labs]


def test_gate_drops_value_not_in_its_span():
    doc = _lab_doc()
    ext = StubExtractor().extract(doc)
    # keep a real span (Creatinine) but claim a value that is not in that span
    real = next(l for l in ext.labs if l.analyte.lower() == "creatinine")
    tampered = real.model_copy(update={"value": "1.00"})
    kept, dropped = verify_lab_extraction(doc, type(ext)(doc_id=doc.doc_id, labs=[tampered]))
    assert dropped and "value not present" in dropped[0]["reason"]


# ---- supervisor / worker graph -------------------------------------------
def test_agent_runs_both_workers_and_grounds_everything():
    report = asyncio.run(
        evidence_agent.run(docs=[_lab_doc(), _intake_doc()], retriever=_retriever())
    )
    # extractor produced grounded facts
    assert report.stats["labs"] >= 5
    assert report.stats["medications"] >= 2
    assert report.extraction.lab_results
    assert all(
        f.source_id in report.extraction.known_source_ids()
        for f in report.extraction.lab_results + report.extraction.medications
    )
    # retriever produced cited evidence relevant to the extracted CKD/metformin picture
    assert report.evidence
    assert all(h.citation and h.url for h in report.evidence)
    # supervisor actually ran both workers
    assert report.query_used


def test_supervisor_routes_question_only_to_retriever():
    report = asyncio.run(
        evidence_agent.run(question="metformin dosing with low eGFR", retriever=_retriever())
    )
    assert report.stats["labs"] == 0  # no docs -> extractor not run
    assert "kdigo-ckd-metformin" in [h.id for h in report.evidence]
