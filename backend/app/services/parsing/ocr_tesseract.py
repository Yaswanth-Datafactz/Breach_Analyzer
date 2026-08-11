"""Tesseract 5 OCR. Copied verbatim from Document_Extraction/backend/app/
services/parsing/ocr_tesseract.py (UC2). TSV output gives per-word
confidence, which is load-bearing for the low-OCR-confidence -> tier-2
vision escalation rule (docs/plan.md §9) -- a plain string-only OCR call
would lose exactly that signal.
"""

from __future__ import annotations

import pytesseract
from PIL import Image

from app.services.parsing.base import PageText, WordBox

# --oem 1: LSTM engine only.
# --psm 6: assume a single uniform block of text -- the right default for
# the memo/letter/report pages this corpus is built from (not a single
# line, not sparse text).
_TESSERACT_CONFIG = "--oem 1 --psm 6"


def run_ocr(image: Image.Image) -> tuple[PageText, float]:
    """Returns (PageText, mean_word_confidence). Words with Tesseract's
    conf == -1 (non-text detections: lines, images, whitespace-only boxes)
    are excluded from both the word list and the mean -- including them
    would silently pull the confidence average toward meaningless values."""
    width, height = image.size
    data = pytesseract.image_to_data(image, config=_TESSERACT_CONFIG, output_type=pytesseract.Output.DICT)

    words: list[WordBox] = []
    confidences: list[float] = []
    lines: dict[tuple[int, int, int], list[str]] = {}

    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue

        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(
            WordBox(
                text=text,
                x0=x / width if width else 0.0,
                y0=y / height if height else 0.0,
                x1=(x + w) / width if width else 0.0,
                y1=(y + h) / height if height else 0.0,
                conf=conf,
            )
        )
        confidences.append(conf)

        line_key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(line_key, []).append(text)

    full_text = "\n".join(" ".join(words_in_line) for words_in_line in lines.values())
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.0

    return PageText(text=full_text, words=words, parser_used="tesseract"), mean_conf
