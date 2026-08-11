"""Digital-PDF text extraction via PyMuPDF. Copied verbatim from
Document_Extraction/backend/app/services/parsing/pdf_text.py (UC2) -- fast,
exact, and gives word-level bounding boxes for free; no OCR is ever run on a
page this module is used for (docs/plan.md §3's pdf_digital path).
"""

from __future__ import annotations

import pymupdf as fitz  # the `fitz` alias is deprecated as of pymupdf 1.28

from app.services.parsing.base import PageText, WordBox


def extract_page_text(page: fitz.Page) -> PageText:
    text = page.get_text()
    rect = page.rect
    width, height = rect.width, rect.height

    words = []
    if width > 0 and height > 0:
        # get_text("words") -> (x0, y0, x1, y1, word, block_no, line_no, word_no)
        # in page-point coordinates -- normalized here to 0-1 so this shape
        # is identical to Tesseract's (services/parsing/ocr_tesseract.py).
        for x0, y0, x1, y1, word, *_ in page.get_text("words"):
            words.append(
                WordBox(
                    text=word,
                    x0=x0 / width,
                    y0=y0 / height,
                    x1=x1 / width,
                    y1=y1 / height,
                    conf=None,
                )
            )

    return PageText(text=text, words=words, parser_used="pdf_text")


def extract_document_text(doc: fitz.Document) -> list[PageText]:
    return [extract_page_text(page) for page in doc]
