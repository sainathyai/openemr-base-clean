"""Deterministic PDF -> words+bounding-boxes, with an OCR fallback (Week 2).

Born-digital pages are read straight out of the PDF text layer (PyMuPDF), which
gives exact word bounding boxes for free. Scanned/faxed pages have no text layer,
so those pages are rasterized and run through Tesseract, whose word boxes are
scaled back into PDF points. Either way the output is uniform: a list of `Word`s
per page, each with a `BBox`, so downstream code never cares how the text was
obtained.

No LLM here. This is the deterministic substrate the extractor worker reasons
over; every fact it later emits can be traced to a `Word`/`BBox` on a page.
"""
from __future__ import annotations

import io
from functools import lru_cache
from typing import Optional, Union

import fitz  # PyMuPDF
from pydantic import BaseModel

from .schemas import BBox, DocType, DocumentSpan

# Rasterize scanned pages at ~300 DPI for OCR (PDF user space is 72 DPI).
_OCR_DPI = 300
_OCR_ZOOM = _OCR_DPI / 72.0
_OCR_MIN_CONF = 40  # drop Tesseract tokens below this confidence


class Word(BaseModel):
    text: str
    bbox: BBox
    ocr: bool = False


class ParsedPage(BaseModel):
    page: int
    width: float
    height: float
    words: list[Word]
    source: str  # "text" | "ocr" | "empty"

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class ParsedDocument(BaseModel):
    doc_id: str
    doc_type: DocType
    pages: list[ParsedPage]
    warnings: list[str] = []

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    def find_span(self, phrase: str, page: Optional[int] = None) -> Optional[DocumentSpan]:
        """Locate `phrase` and return a DocumentSpan whose bbox covers it.

        Matches a contiguous run of words (case/space-insensitive). Returns the
        first hit; the bbox is the union of the matched words' boxes. This is how
        an extracted value earns its pixel-accurate citation.
        """
        target = _norm(phrase)
        if not target:
            return None
        tokens = target.split()
        for p in self.pages:
            if page is not None and p.page != page:
                continue
            norm_words = [_norm(w.text) for w in p.words]
            for i in range(len(norm_words) - len(tokens) + 1):
                if norm_words[i : i + len(tokens)] == tokens:
                    run = p.words[i : i + len(tokens)]
                    return DocumentSpan(
                        doc_id=self.doc_id,
                        doc_type=self.doc_type,
                        page=p.page,
                        text=" ".join(w.text for w in run),
                        bbox=_union([w.bbox for w in run]),
                        ocr=any(w.ocr for w in run),
                    )
        return None


@lru_cache(maxsize=1)
def ocr_available() -> bool:
    """True iff the Tesseract binary is reachable (image has it; local venv may not)."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def parse_pdf(
    source: Union[str, bytes],
    doc_id: str,
    doc_type: DocType,
) -> ParsedDocument:
    """Parse a PDF (path or bytes) into pages of words+bboxes, OCR-ing scans."""
    if isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    else:
        doc = fitz.open(source)

    pages: list[ParsedPage] = []
    warnings: list[str] = []
    try:
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            rect = page.rect
            words = _words_from_text_layer(page, pno)
            source_kind = "text"
            if not words:
                if ocr_available():
                    words = _words_from_ocr(page, pno)
                    source_kind = "ocr" if words else "empty"
                    if not words:
                        warnings.append(f"page {pno}: OCR produced no text")
                else:
                    source_kind = "empty"
                    warnings.append(
                        f"page {pno}: no text layer and OCR unavailable "
                        f"(Tesseract not installed) — page skipped"
                    )
            pages.append(
                ParsedPage(
                    page=pno,
                    width=rect.width,
                    height=rect.height,
                    words=words,
                    source=source_kind,
                )
            )
    finally:
        doc.close()

    return ParsedDocument(doc_id=doc_id, doc_type=doc_type, pages=pages, warnings=warnings)


def _words_from_text_layer(page: "fitz.Page", pno: int) -> list[Word]:
    out: list[Word] = []
    # get_text("words") -> (x0, y0, x1, y1, word, block_no, line_no, word_no)
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if not text.strip():
            continue
        out.append(
            Word(text=text, bbox=BBox(page=pno, x0=x0, y0=y0, x1=x1, y1=y1), ocr=False)
        )
    return out


def _words_from_ocr(page: "fitz.Page", pno: int) -> list[Word]:
    import pytesseract
    from PIL import Image

    pix = page.get_pixmap(matrix=fitz.Matrix(_OCR_ZOOM, _OCR_ZOOM))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    out: list[Word] = []
    for i, text in enumerate(data["text"]):
        if not text.strip():
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1.0
        if conf < _OCR_MIN_CONF:
            continue
        # pixel coords -> PDF points
        x0 = data["left"][i] / _OCR_ZOOM
        y0 = data["top"][i] / _OCR_ZOOM
        x1 = x0 + data["width"][i] / _OCR_ZOOM
        y1 = y0 + data["height"][i] / _OCR_ZOOM
        out.append(
            Word(text=text, bbox=BBox(page=pno, x0=x0, y0=y0, x1=x1, y1=y1), ocr=True)
        )
    return out


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _union(boxes: list[BBox]) -> BBox:
    return BBox(
        page=boxes[0].page,
        x0=min(b.x0 for b in boxes),
        y0=min(b.y0 for b in boxes),
        x1=max(b.x1 for b in boxes),
        y1=max(b.y1 for b in boxes),
    )
