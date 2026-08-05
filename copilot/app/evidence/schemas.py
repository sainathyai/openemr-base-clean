"""Typed contracts for document ingestion and extraction (Week 2).

Two layers:

1. **Provenance** — `BBox` / `DocumentSpan` locate a fact in the source document
   (page + bounding box + the exact supporting text). A span serialises to a
   `source_id` string of the form ``doc:<doc_id>#p<page>#<span_hash>`` so it drops
   straight into the Week-1 fact models (`Problem.source_id`, etc.) and the
   verification gate can existence-check it exactly like a FHIR resource id.

2. **Extraction schemas** — strict Pydantic per document type (lab report, intake
   form). These are the ONLY shapes the extractor worker is allowed to emit; the
   LLM fills fields, never invents provenance. Every extracted item carries its
   `span` and a `confidence`, and maps deterministically onto the existing
   `PatientContext` fact models via `to_context_facts`.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..schemas import Allergy, LabResult, Medication, Problem

DocType = Literal["lab_report", "intake_form"]

# source_id grammar for a document-derived fact.
_SOURCE_RE = re.compile(r"^doc:(?P<doc>[^#]+)#p(?P<page>\d+)#(?P<span>[0-9a-f]{8})$")


class BBox(BaseModel):
    """Bounding box in PDF points (origin top-left), 0-based page index."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class DocumentSpan(BaseModel):
    """Where a fact came from: the document, the page, the box, the exact text."""

    doc_id: str
    doc_type: DocType
    page: int
    text: str                       # the exact substring supporting the fact
    bbox: Optional[BBox] = None     # None when OCR could not localise the token
    ocr: bool = False               # True if this text came from the OCR fallback

    @property
    def span_hash(self) -> str:
        """Stable 8-hex id for the fact within its page (bbox + text)."""
        key = f"{self.bbox.as_tuple() if self.bbox else 'noloc'}|{self.text.strip()}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]

    @property
    def source_id(self) -> str:
        return f"doc:{self.doc_id}#p{self.page}#{self.span_hash}"


def parse_source_id(source_id: str) -> Optional[dict]:
    """Decompose a document source_id; None if it is not a document ref."""
    m = _SOURCE_RE.match(source_id)
    if not m:
        return None
    return {"doc_id": m["doc"], "page": int(m["page"]), "span": m["span"]}


def is_document_source(source_id: str) -> bool:
    return _SOURCE_RE.match(source_id) is not None


# --------------------------------------------------------------------------- #
# Extraction schemas (the extractor worker's ONLY allowed output shapes).
# --------------------------------------------------------------------------- #
class ExtractedLab(BaseModel):
    """One analyte result read off a lab report."""

    analyte: str
    loinc: Optional[str] = None
    value: Optional[str] = None          # string: values are not always numeric
    unit: Optional[str] = None
    reference_range: Optional[str] = None  # as printed; NOT trusted for abnormality
    collected: Optional[date] = None
    span: DocumentSpan
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class ExtractedStatement(BaseModel):
    """A free-text clinical statement off an intake form (med/allergy/problem)."""

    kind: Literal["medication", "allergy", "problem"]
    text: str
    span: DocumentSpan
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class LabReportExtraction(BaseModel):
    doc_id: str
    doc_type: Literal["lab_report"] = "lab_report"
    patient_hint: Optional[str] = None   # name/dob as printed, for a soft match
    report_date: Optional[date] = None
    labs: list[ExtractedLab] = Field(default_factory=list)


class IntakeFormExtraction(BaseModel):
    doc_id: str
    doc_type: Literal["intake_form"] = "intake_form"
    patient_hint: Optional[str] = None
    chief_complaint: Optional[str] = None
    statements: list[ExtractedStatement] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """What the evidence agent hands back: typed facts + a span registry.

    `spans` maps every emitted fact's source_id to its DocumentSpan so the
    verification gate can existence-check ids and the UI can draw the bbox.
    """

    lab_results: list[LabResult] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    spans: dict[str, DocumentSpan] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def known_source_ids(self) -> set[str]:
        return set(self.spans.keys())


def to_context_facts(
    labs: Optional[LabReportExtraction] = None,
    intake: Optional[IntakeFormExtraction] = None,
) -> ExtractionResult:
    """Map typed extractions onto Week-1 fact models with document source_ids.

    Deterministic and LLM-free: the extractor produced the typed values; this
    only re-shapes them and stamps each with `span.source_id`, registering the
    span so the fact is attributable and verifiable exactly like a FHIR fact.
    """
    out = ExtractionResult()

    if labs is not None:
        for lab in labs.labs:
            sid = lab.span.source_id
            out.spans[sid] = lab.span
            out.lab_results.append(
                LabResult(
                    source_id=sid,
                    loinc=lab.loinc,
                    name=lab.analyte,
                    value=lab.value,
                    unit=lab.unit,
                    effective=None if lab.collected is None else _as_dt(lab.collected),
                )
            )

    if intake is not None:
        for st in intake.statements:
            sid = st.span.source_id
            out.spans[sid] = st.span
            if st.kind == "medication":
                out.medications.append(Medication(source_id=sid, text=st.text))
            elif st.kind == "allergy":
                out.allergies.append(Allergy(source_id=sid, text=st.text))
            else:
                out.problems.append(Problem(source_id=sid, text=st.text))

    return out


def _as_dt(d: date):
    from datetime import datetime, time

    return datetime.combine(d, time())
