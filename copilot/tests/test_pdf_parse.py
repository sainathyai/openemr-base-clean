"""Slice 2: deterministic PDF parsing (text path) + bbox span location.

The OCR path needs the Tesseract binary, which lives in the image, not the local
venv; those assertions are skipped when the binary is absent (verified in-container).
"""
import pytest

from app.evidence.pdf_parse import ocr_available, parse_pdf
from app.evidence.sample_docs import intake_form_pdf, lab_report_pdf, scanned_pdf


def test_born_digital_lab_report_parses_via_text_layer():
    doc = parse_pdf(lab_report_pdf(), doc_id="lab-001", doc_type="lab_report")
    assert doc.pages and doc.pages[0].source == "text"
    assert "Creatinine" in doc.full_text
    assert "2.10" in doc.full_text
    # every word carries a real bounding box
    words = doc.pages[0].words
    assert words
    assert all(w.bbox.x1 > w.bbox.x0 and w.bbox.y1 > w.bbox.y0 for w in words)


def test_find_span_returns_pixel_accurate_citation():
    doc = parse_pdf(lab_report_pdf(), doc_id="lab-001", doc_type="lab_report")
    span = doc.find_span("Creatinine")
    assert span is not None
    assert span.page == 0
    assert span.bbox is not None
    assert span.text.lower().startswith("creatinine")
    # the source_id is a well-formed, checkable document reference
    from app.evidence.schemas import is_document_source

    assert is_document_source(span.source_id)


def test_find_span_matches_multi_word_run():
    doc = parse_pdf(intake_form_pdf(), doc_id="intake-7", doc_type="intake_form")
    span = doc.find_span("Penicillin - rash")
    assert span is not None
    # union box spans all three tokens
    assert span.bbox.x1 > span.bbox.x0
    assert "penicillin" in span.text.lower()


def test_find_span_absent_phrase_returns_none():
    doc = parse_pdf(lab_report_pdf(), doc_id="lab-001", doc_type="lab_report")
    assert doc.find_span("Troponin") is None


def test_scanned_pdf_has_no_text_layer():
    scanned = scanned_pdf(lab_report_pdf())
    doc = parse_pdf(scanned, doc_id="lab-001-scan", doc_type="lab_report")
    if ocr_available():
        # OCR should recover the headline analyte
        assert "creatinine" in doc.full_text.lower()
        assert doc.pages[0].source == "ocr"
    else:
        # graceful degradation: no crash, page flagged empty, clear warning
        assert doc.pages[0].source == "empty"
        assert any("OCR unavailable" in w for w in doc.warnings)


@pytest.mark.skipif(not ocr_available(), reason="Tesseract binary not installed (lives in image)")
def test_ocr_path_yields_boxed_words():
    scanned = scanned_pdf(intake_form_pdf())
    doc = parse_pdf(scanned, doc_id="intake-scan", doc_type="intake_form")
    assert doc.pages[0].source == "ocr"
    assert doc.pages[0].words
    assert all(w.ocr for w in doc.pages[0].words)
