"""PNG parsing (corpusgen's screenshot-style renderer and the bulk-
spreadsheet 'evil twin' PNG): a standalone image is by definition
image-based -- one OCR'd `page` passage, page 1.

The original PNG bytes are cached as the document's page-1 image
(storage.store_page_image keyed by the document sha256, same convention as
services/parsing/pdf.py) so the PassageViewer and a tier-2 vision
escalation both read the identical bytes the OCR saw, and the passage
carries ocr=True + the measured mean word confidence for the low-
confidence -> vision escalation rule (plan §9).
"""

from __future__ import annotations

import io

from PIL import Image

from app.services.parsing.images import preprocess
from app.services.parsing.ocr_tesseract import run_ocr
from app.services.parsing.storage import store_page_image
from app.services.parsing.types import ParsedPassage, ParseResult


def parse_png(content: bytes, *, sha256: str) -> ParseResult:
    """Raises on undecodable input (PIL refuses it) -- mapped to a
    `corrupt` quarantine by the pipeline's exception boundary."""
    image = Image.open(io.BytesIO(content))
    image.load()

    store_page_image(content, sha256, 1)

    ocr_result, mean_conf = run_ocr(preprocess(image.convert("RGB")).image)
    passage = ParsedPassage(
        kind="page",
        locator={"page": 1, "ocr_mean_conf": round(mean_conf, 1)},
        text=ocr_result.text,
        ocr=True,
        page_image_sha=sha256,
    )
    return ParseResult(passages=[passage], page_count=1, is_image_based=True)
