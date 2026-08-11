"""Shared shapes for both parsing paths. Copied verbatim from
Document_Extraction/backend/app/services/parsing/base.py (UC2). `WordBox`
uses the SAME normalized (0-1) coordinate shape whether it came from
PyMuPDF's digital-text extraction or Tesseract's OCR -- that uniformity is
what lets evidence anchoring treat both paths identically, and is why `conf`
is `None` (not 100) for digital text: a digital word is not "OCR confidence
100", it is a different kind of certainty the confidence composite must not
conflate with a real measured OCR confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WordBox:
    text: str
    x0: float  # normalized 0-1, left
    y0: float  # normalized 0-1, top
    x1: float  # normalized 0-1, right
    y1: float  # normalized 0-1, bottom
    conf: float | None = None  # Tesseract word confidence 0-100; None for digital text


@dataclass(frozen=True)
class PageText:
    text: str
    words: list[WordBox] = field(default_factory=list)
    parser_used: str = "unknown"  # 'pdf_text' | 'tesseract'
