"""Rasterization + preprocessing for the scanned/OCR path. Copied verbatim
from Document_Extraction/backend/app/services/parsing/images.py (UC2).
Skipped entirely for digital pages -- every transform here is recorded by
the caller so a bad OCR result is attributable to a specific preprocessing
step, not a black box (the corpus's scanned PDFs are deliberately rotated
0.5-2 degrees + noised, docs/plan.md §8, so the deskew here has real work).
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import pymupdf as fitz  # the `fitz` alias is deprecated as of pymupdf 1.28
import numpy as np
from PIL import Image


def rasterize_pdf_page(page: fitz.Page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@dataclass(frozen=True)
class PreprocessResult:
    image: Image.Image  # grayscale, deskewed, denoised
    deskew_angle: float


def _measure_deskew_angle(gray: np.ndarray) -> float:
    """Standard OpenCV minAreaRect deskew measurement: threshold to a binary
    foreground mask, then fit the smallest rotated rectangle around all
    foreground pixels. Returns degrees to rotate to correct the skew (may be
    0.0 on a page with too little foreground to measure confidently)."""
    _, binarized = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(binarized > 0))
    if coords.shape[0] < 20:  # not enough foreground to measure confidently
        return 0.0

    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    # Guard against wild misfires on near-blank pages producing a huge angle
    # that would make things worse, not better.
    if abs(angle) > 45:
        return 0.0
    return float(angle)


def preprocess(image: Image.Image) -> PreprocessResult:
    """Grayscale -> Otsu binarize (for deskew measurement only) -> deskew
    rotation -> light median denoise. The RETURNED image is grayscale +
    deskewed + denoised, not binarized -- Tesseract's own internal
    binarization generally outperforms a naive global Otsu pass baked in
    ahead of time, so we hand it a clean grayscale image and let it do that
    step itself."""
    gray = np.array(image.convert("L"))

    angle = _measure_deskew_angle(gray)
    if angle != 0.0:
        h, w = gray.shape
        center = (w / 2, h / 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        gray = cv2.warpAffine(
            gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255
        )

    denoised = cv2.medianBlur(gray, 3)

    return PreprocessResult(image=Image.fromarray(denoised), deskew_angle=angle)
