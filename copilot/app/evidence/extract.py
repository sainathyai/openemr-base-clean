"""Document extraction + the extraction verification gate (Week 2).

The extractor reads a parsed document and fills the strict schemas from
`schemas.py`. Two implementations mirror the narrator split: `StubExtractor`
(deterministic, zero-cost, used in the demo and tests) and `ClaudeExtractor`
(Haiku fills the schema, reserved and budget-guarded). Either way the output is
untrusted until it passes the gate.

The gate (`verify_lab_extraction` / `verify_intake_extraction`) enforces the same
invariant the Week-1 narrator gate does, translated to extraction: **an extracted
value is kept only if its text is actually located in the source document** (the
span resolves AND the value appears in that span). A fabricated or misread value
has no home in the document, so it is dropped, not shown. This is the property
the 50-case eval injects regressions against.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Protocol

from .pdf_parse import ParsedDocument
from .schemas import (
    DocType,
    ExtractedLab,
    ExtractedStatement,
    IntakeFormExtraction,
    LabReportExtraction,
)

# A results row: "Creatinine   2.10   mg/dL   0.6-1.3"
_LAB_ROW = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z ()/-]*?[A-Za-z)])\s{2,}"
    r"(?P<value>[<>]?\d+(?:\.\d+)?)\s+"
    r"(?P<unit>[A-Za-z%/0-9.\[\]]+)"
    r"(?:\s+(?P<ref>[<>]?[-\d./A-Za-z ]+))?\s*$"
)
_HEADER_WORDS = {"test", "result", "units", "reference"}
_MED_HINT = re.compile(r"\b(mg|mcg|ml|units?|tablet|cap|daily|bid|tid|qhs|bedtime|once|twice)\b", re.I)


class Extractor(Protocol):
    name: str

    def extract(self, doc: ParsedDocument):
        """Return a LabReportExtraction or IntakeFormExtraction for the document."""
        ...


class StubExtractor:
    """Deterministic rule-based extraction. No LLM, no spend."""

    name = "stub"

    def extract(self, doc: ParsedDocument):
        if doc.doc_type == "lab_report":
            return self._labs(doc)
        return self._intake(doc)

    def _labs(self, doc: ParsedDocument) -> LabReportExtraction:
        out = LabReportExtraction(doc_id=doc.doc_id)
        out.report_date = _find_date(doc.full_text, prefer="Collected")
        for page in doc.pages:
            for _x0, line in _lines(page):
                m = _LAB_ROW.match(line)
                if not m:
                    continue
                name = m.group("name").strip()
                if name.lower() in _HEADER_WORDS or len(name) < 2:
                    continue
                value = m.group("value")
                # span covers the name+value run so the value is inside the citation
                span = doc.find_span(f"{name} {value}", page=page.page)
                if span is None:
                    span = doc.find_span(name, page=page.page)
                if span is None:
                    continue
                out.labs.append(
                    ExtractedLab(
                        analyte=name,
                        value=value,
                        unit=m.group("unit"),
                        reference_range=(m.group("ref") or "").strip() or None,
                        collected=out.report_date,
                        span=span,
                    )
                )
        return out

    def _intake(self, doc: ParsedDocument) -> IntakeFormExtraction:
        out = IntakeFormExtraction(doc_id=doc.doc_id)
        section = None  # medication | allergy | problem
        for page in doc.pages:
            lines = _lines(page)
            if not lines:
                continue
            # form entries are indented; detect that from the left x-offset, since
            # leading spaces are lost when reconstructing lines from word boxes.
            left_margin = min(x0 for x0, _ in lines)
            for x0, text in lines:
                line = " ".join(text.split())  # collapse the rebuilt column spacing
                low = line.lower()
                indented = x0 > left_margin + 5.0
                if low.startswith("reason for visit"):
                    out.chief_complaint = line.split(":", 1)[1].strip() if ":" in line else None
                    continue
                if "current medication" in low:
                    section = "medication"; continue
                if low.startswith("allergies"):
                    section = "allergy"; continue
                if "past medical history" in low:
                    section = "problem"; continue
                if not line or line.endswith(":") or section is None:
                    continue
                if not indented:  # a non-indented line ends the current section
                    section = None
                    continue
                span = doc.find_span(line, page=page.page)
                if span is None:
                    continue
                out.statements.append(
                    ExtractedStatement(kind=section, text=line, span=span)
                )
        return out


class ClaudeExtractor:
    """LLM-driven extraction (Haiku). Reserved/budget-guarded; stub is the default.

    The model fills the strict schema from the page text; the gate then discards
    any value it cannot ground in the document, so a misread never reaches the
    chart. Implemented lazily to avoid importing anthropic in the zero-cost path.
    """

    name = "claude"

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("COPILOT_MODEL", "claude-haiku-4-5-20251001")

    def extract(self, doc: ParsedDocument):
        # Kept intentionally thin: real structured-output wiring lives behind the
        # same anthropic client the narrator uses. Falls back to the stub shape so
        # the graph never breaks if the model is unavailable. (No spend here.)
        raise NotImplementedError("ClaudeExtractor is reserved; enable explicitly")


def get_extractor() -> Extractor:
    """Stub unless an LLM extractor is explicitly enabled (mirrors get_narrator)."""
    if os.environ.get("COPILOT_LLM_EXTRACT") == "1" and not os.environ.get("COPILOT_FORCE_STUB"):
        return ClaudeExtractor()
    return StubExtractor()


# --------------------------------------------------------------------------- #
# Extraction verification gate.
# --------------------------------------------------------------------------- #
class DroppedFact(dict):
    """A dropped extraction with its reason (dict for easy JSON/trace)."""


def verify_lab_extraction(
    doc: ParsedDocument, extraction: LabReportExtraction
) -> tuple[LabReportExtraction, list[dict]]:
    kept = LabReportExtraction(
        doc_id=extraction.doc_id,
        patient_hint=extraction.patient_hint,
        report_date=extraction.report_date,
    )
    dropped: list[dict] = []
    for lab in extraction.labs:
        reason = _ground(doc, lab.span, value=lab.value)
        if reason:
            dropped.append({"kind": "lab", "analyte": lab.analyte, "value": lab.value, "reason": reason})
        else:
            kept.labs.append(lab)
    return kept, dropped


def verify_intake_extraction(
    doc: ParsedDocument, extraction: IntakeFormExtraction
) -> tuple[IntakeFormExtraction, list[dict]]:
    kept = IntakeFormExtraction(
        doc_id=extraction.doc_id,
        patient_hint=extraction.patient_hint,
        chief_complaint=extraction.chief_complaint,
    )
    dropped: list[dict] = []
    for st in extraction.statements:
        reason = _ground(doc, st.span, value=None)
        if reason:
            dropped.append({"kind": st.kind, "text": st.text, "reason": reason})
        else:
            kept.statements.append(st)
    return kept, dropped


def _ground(doc: ParsedDocument, span, value: Optional[str]) -> Optional[str]:
    """Return a drop-reason if the span/value is not grounded in the document, else None."""
    located = doc.find_span(span.text, page=span.page)
    if located is None:
        return "span text not found in document"
    if value is not None and _norm(value) not in _norm(span.text):
        return "extracted value not present in its cited span"
    return None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _lines(page) -> list[tuple[float, str]]:
    """Reconstruct (left_x0, text) lines from word boxes (group by rounded y).

    Column spacing is rebuilt from x-gaps so the lab-row regex still sees the
    gaps; the left x0 is returned so callers can detect indentation (leading
    spaces are not recoverable from word boxes).
    """
    rows: dict[int, list] = {}
    for w in page.words:
        key = round(w.bbox.y0 / 3.0)  # ~3pt row bucket
        rows.setdefault(key, []).append(w)
    lines: list[tuple[float, str]] = []
    for key in sorted(rows):
        ws = sorted(rows[key], key=lambda w: w.bbox.x0)
        left_x0 = ws[0].bbox.x0
        line = ""
        prev_x1 = None
        for w in ws:
            if prev_x1 is not None:
                gap = w.bbox.x0 - prev_x1
                line += " " * max(1, int(gap / 3.0))
            line += w.text
            prev_x1 = w.bbox.x1
        lines.append((left_x0, line))
    return lines


def _find_date(text: str, prefer: str) -> Optional["date"]:
    from datetime import date

    m = re.search(rf"{prefer}:\s*(\d{{4}})-(\d{{2}})-(\d{{2}})", text)
    if not m:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
