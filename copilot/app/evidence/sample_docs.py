"""Synthetic clinical documents for development and demo (Week 2).

Born-digital PDFs built with PyMuPDF so they have a real text layer (exercises
the fast path) while remaining deterministic and safe to commit. Content is
authored to line up with the existing Synthea patient story (Pfeffer: CKD +
elevated creatinine) so extracted facts can be reconciled against the FHIR
record in the demo. A scanned variant (image-only, forces the OCR path) is
produced by rasterizing the born-digital page.
"""
from __future__ import annotations

import io

import fitz  # PyMuPDF

_MARGIN = 56.0
_LETTER = (612.0, 792.0)


def lab_report_pdf(patient: str = "Pfeffer, Aaron", dob: str = "1972-04-18") -> bytes:
    """A lab report with an elevated creatinine / low eGFR story."""
    lines = [
        ("VERITAS REGIONAL LABORATORY", 16, True),
        ("123 Halsted St, Chicago IL  ·  CLIA 14D0000000", 9, False),
        ("", 6, False),
        (f"Patient: {patient}      DOB: {dob}", 11, False),
        ("Collected: 2026-03-01     Reported: 2026-03-02", 11, False),
        ("Ordering provider: Gregory House, MD", 11, False),
        ("", 8, False),
        ("COMPREHENSIVE METABOLIC PANEL", 12, True),
        ("Test                     Result     Units          Reference", 10, False),
        ("Creatinine               2.10       mg/dL          0.6-1.3", 10, False),
        ("eGFR                     38         mL/min/1.73    >60", 10, False),
        ("Potassium                5.1        mmol/L         3.5-5.1", 10, False),
        ("Sodium                   139        mmol/L         135-145", 10, False),
        ("Glucose                  142        mg/dL          70-99", 10, False),
        ("", 6, False),
        ("HEMATOLOGY", 12, True),
        ("Hemoglobin               10.8       g/dL           13.5-17.5", 10, False),
        ("Hematocrit               33.1       %              41-53", 10, False),
    ]
    return _render(lines)


def intake_form_pdf(patient: str = "Pfeffer, Aaron") -> bytes:
    """A patient intake form: chief complaint, home meds, allergies."""
    lines = [
        ("PATIENT INTAKE FORM", 16, True),
        ("Sunrise Family Medicine", 9, False),
        ("", 8, False),
        (f"Name: {patient}      Date: 2026-03-04", 11, False),
        ("", 6, False),
        ("Reason for visit: Follow-up, increasing fatigue and ankle swelling", 11, False),
        ("", 8, False),
        ("Current medications (please list all):", 12, True),
        ("  lisinopril 10 mg once daily", 10, False),
        ("  atorvastatin 40 mg at bedtime", 10, False),
        ("  metformin 1000 mg twice daily", 10, False),
        ("", 8, False),
        ("Allergies:", 12, True),
        ("  Penicillin - rash", 10, False),
        ("  Sulfa - hives", 10, False),
        ("", 8, False),
        ("Past medical history:", 12, True),
        ("  Chronic kidney disease", 10, False),
        ("  Hypertension", 10, False),
    ]
    return _render(lines)


def scanned_pdf(born_digital: bytes, dpi: int = 200) -> bytes:
    """Turn a text-layer PDF into an image-only PDF (no text) to force OCR."""
    src = fitz.open(stream=born_digital, filetype="pdf")
    out = fitz.open()
    try:
        for pno in range(src.page_count):
            page = src.load_page(pno)
            pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0))
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=pix.tobytes("png"))
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()
    finally:
        src.close()
        out.close()


def _render(lines: list[tuple[str, float, bool]]) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=_LETTER[0], height=_LETTER[1])
    y = _MARGIN
    for text, size, bold in lines:
        if text:
            font = "Courier-Bold" if bold else "Courier"
            page.insert_text((_MARGIN, y), text, fontsize=size, fontname=font)
        y += size + 6
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()
