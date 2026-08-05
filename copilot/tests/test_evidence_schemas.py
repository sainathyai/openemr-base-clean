"""Slice 1: document provenance contract + extraction-schema mapping."""
from datetime import date

from app.evidence.schemas import (
    BBox,
    DocumentSpan,
    ExtractedLab,
    ExtractedStatement,
    IntakeFormExtraction,
    LabReportExtraction,
    is_document_source,
    parse_source_id,
    to_context_facts,
)


def _span(doc="lab-001", page=0, text="Creatinine 2.10 mg/dL", box=(72, 140, 300, 152)):
    return DocumentSpan(
        doc_id=doc,
        doc_type="lab_report",
        page=page,
        text=text,
        bbox=BBox(page=page, x0=box[0], y0=box[1], x1=box[2], y1=box[3]),
    )


def test_source_id_roundtrips_and_is_document():
    sid = _span().source_id
    assert is_document_source(sid)
    parsed = parse_source_id(sid)
    assert parsed["doc_id"] == "lab-001"
    assert parsed["page"] == 0
    assert len(parsed["span"]) == 8


def test_source_id_is_stable_and_location_sensitive():
    a = _span().source_id
    b = _span().source_id
    assert a == b  # same bbox + text -> same id
    moved = _span(box=(72, 200, 300, 212)).source_id
    assert moved != a  # different location -> different id


def test_fhir_ids_are_not_document_sources():
    assert not is_document_source("Observation/1234")
    assert parse_source_id("MedicationRequest/9") is None


def test_lab_extraction_maps_to_context_with_span_registry():
    span = _span()
    labs = LabReportExtraction(
        doc_id="lab-001",
        report_date=date(2026, 3, 1),
        labs=[
            ExtractedLab(
                analyte="Creatinine",
                loinc="2160-0",
                value="2.10",
                unit="mg/dL",
                reference_range="0.6-1.3",
                collected=date(2026, 3, 1),
                span=span,
            )
        ],
    )
    res = to_context_facts(labs=labs)
    assert len(res.lab_results) == 1
    lab = res.lab_results[0]
    assert lab.name == "Creatinine"
    assert lab.value == "2.10"
    # the emitted fact's source_id is registered -> verification gate can check it
    assert lab.source_id in res.known_source_ids()
    assert res.spans[lab.source_id].bbox is not None


def test_intake_statements_split_by_kind():
    span = DocumentSpan(
        doc_id="intake-7", doc_type="intake_form", page=0, text="Penicillin - rash"
    )
    intake = IntakeFormExtraction(
        doc_id="intake-7",
        chief_complaint="Follow-up",
        statements=[
            ExtractedStatement(kind="allergy", text="Penicillin - rash", span=span),
            ExtractedStatement(
                kind="medication",
                text="lisinopril 10 mg daily",
                span=DocumentSpan(
                    doc_id="intake-7", doc_type="intake_form", page=0,
                    text="lisinopril 10 mg daily",
                ),
            ),
        ],
    )
    res = to_context_facts(intake=intake)
    assert len(res.allergies) == 1 and res.allergies[0].text == "Penicillin - rash"
    assert len(res.medications) == 1
    # OCR-unlocalised span (no bbox) still yields a valid, checkable source_id
    assert all(is_document_source(sid) for sid in res.known_source_ids())
