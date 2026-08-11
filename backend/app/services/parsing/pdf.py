"""PDF parsing: digital text per page via the existing pdf_text module,
with the plan §3 image-based heuristic routing individual pages to OCR.

Heuristic (per page, config-driven -- core/config.py's ocr_* settings):
fewer than `ocr_min_chars_per_page` extractable chars OR a garbage ratio
above `ocr_max_garbage_ratio` means the page's text layer is absent or
junk, so the page is rasterized (render_dpi), preprocessed (deskew +
denoise -- corpusgen's scanned PDFs are deliberately rotated 0.5-2 deg),
and OCR'd. OCR'd passages carry ocr=True and page_image_sha (the DOCUMENT
sha256 -- storage.page_image_path(doc_sha, page_no) is how the
PassageViewer retrieves the cached render; the page number rides in the
locator). The stored image is the RAW render, not the preprocessed one:
the viewer should show the page as it is, and re-running OCR with
different preprocessing must start from the original.

The document is is_image_based when at least half its pages OCR'd -- the
pipeline flips file_class pdf_digital -> pdf_scanned off this flag (the
enum split classify.py deliberately defers to here, where pages have
actually been inspected).
"""

from __future__ import annotations

import io

import pymupdf as fitz  # the `fitz` alias is deprecated as of pymupdf 1.28

from app.core.config import get_settings
from app.services.parsing.images import preprocess, rasterize_pdf_page
from app.services.parsing.ocr_tesseract import run_ocr
from app.services.parsing.pdf_text import extract_page_text
from app.services.parsing.storage import store_page_image
from app.services.parsing.types import ParsedPassage, ParseResult

# Characters that are unremarkable in real prose; everything else counts
# toward the garbage ratio (mojibake, control chars, dingbat soup from a
# broken text layer).
_CLEAN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t\n\r.,;:!?'\"()[]{}<>-_/@#$%&*+=|~^`"
)


def garbage_ratio(text: str) -> float:
    """Fraction of characters outside the plain-prose set; 0.0 for empty
    text (an empty page is 'too little text', not 'garbage' -- the char
    minimum handles it)."""
    if not text:
        return 0.0
    dirty = sum(1 for ch in text if ch not in _CLEAN_CHARS)
    return dirty / len(text)


def _page_needs_ocr(text: str) -> bool:
    settings = get_settings()
    stripped = text.strip()
    if len(stripped) < settings.ocr_min_chars_per_page:
        return True
    return garbage_ratio(stripped) > settings.ocr_max_garbage_ratio


def parse_pdf(content: bytes, *, sha256: str) -> ParseResult:
    """Raises on corrupt input (pymupdf refuses to open it) -- mapped to a
    `corrupt` quarantine by the pipeline's exception boundary. Encrypted
    PDFs never reach here (classify.py's pikepdf probe quarantines them at
    ingest)."""
    settings = get_settings()
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        passages: list[ParsedPassage] = []
        ocr_pages = 0
        for page_no, page in enumerate(doc, start=1):
            page_text = extract_page_text(page)
            if not _page_needs_ocr(page_text.text):
                passages.append(
                    ParsedPassage(kind="page", locator={"page": page_no}, text=page_text.text)
                )
                continue

            image = rasterize_pdf_page(page, settings.render_dpi)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            store_page_image(buffer.getvalue(), sha256, page_no)

            ocr_result, mean_conf = run_ocr(preprocess(image).image)
            ocr_pages += 1
            passages.append(
                ParsedPassage(
                    kind="page",
                    locator={"page": page_no, "ocr_mean_conf": round(mean_conf, 1)},
                    text=ocr_result.text,
                    ocr=True,
                    page_image_sha=sha256,
                )
            )

        page_count = doc.page_count
        is_image_based = page_count > 0 and ocr_pages * 2 >= page_count
        return ParseResult(
            passages=passages, page_count=page_count, is_image_based=is_image_based
        )
    finally:
        doc.close()
