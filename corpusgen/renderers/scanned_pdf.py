"""Scanned-PDF renderer: digital render -> rasterize (PyMuPDF, 200 DPI) ->
degrade (rotate 0.5-2 deg, effective-resolution downscale, gaussian noise,
slight blur — Pillow/numpy) -> image-only PDF (JPEG pages, no text layer).

Degradation is TUNED (spike-measured, Aug 12) so Tesseract lands in the
~80-95% exact-recovery band across the scanned set while EVERY planting
still clears the validator's fuzz>=85 bar: the failure mode is sharp —
below ~0.42 downscale OCR drops whole tokens rather than single chars —
so each doc is OCR-checked at render time and falls back one level
(STRONG -> MILD -> FAINT) until all its plantings clear fuzz>=85. Most
docs stay STRONG (~94% exact there), blending set-wide exact recovery
into the target band.

The render-time check OCRs the IDENTICAL pixels validate.py will see —
the candidate PDF is built in memory and re-rasterized through PyMuPDF at
the validator's DPI (OCRing the pre-embed JPEG instead was measured to
disagree: tesseract's line segmentation flips catastrophically on the
sub-pixel differences insert_image introduces), so renderer acceptance
and validation are the same deterministic function.

Determinism: rotation/noise are seeded from a CRC of the spec content
(never the shared RNG stream — retries must not perturb sibling docs) and
Tesseract is deterministic for a given image, so the level ladder, the
output bytes, and the manifest are all seed-stable. Plantings keep the
digital render's page locations with presentation forced to "image".
"""

from __future__ import annotations

import io
import random
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf
import pytesseract
from PIL import Image, ImageFilter
from rapidfuzz import fuzz

from corpusgen.renderers import FIXED_DOC_DT, DocumentSpec, digital_pdf

DPI = 200
FUZZ_THRESHOLD = 85  # must match validate.py's OCR-tolerant bar


@dataclass(frozen=True)
class DegradeLevel:
    name: str
    rotate: tuple[float, float]  # degrees, sign chosen per page
    downscale: float  # effective-resolution factor (0.40 ~ 80 DPI)
    noise_sigma: float
    blur_radius: float
    jpeg_quality: int


# Ladder order matters: first level whose OCR clears every planting wins.
LEVELS: tuple[DegradeLevel, ...] = (
    DegradeLevel("strong", (0.5, 2.0), 0.40, 12.0, 0.8, 55),
    DegradeLevel("mild", (0.5, 1.5), 0.55, 8.0, 0.6, 65),
    DegradeLevel("faint", (0.3, 0.8), 1.00, 4.0, 0.4, 75),
)


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _spec_seed(spec: DocumentSpec) -> int:
    """Content-derived seed: stable across runs, distinct across docs."""
    parts = [spec.archetype, spec.title, spec.doc_date.isoformat()]
    for block in spec.blocks:
        parts.append(getattr(block, "text", "") or "|".join(
            cell for row in getattr(block, "rows", []) for cell in row
        ))
    return zlib.crc32("\x1f".join(parts).encode())


def degrade_page(img: Image.Image, level: DegradeLevel, seed: int) -> bytes:
    """One page image -> degraded JPEG bytes (grayscale)."""
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    gray = img.convert("L")
    angle = rng.uniform(*level.rotate) * rng.choice([-1.0, 1.0])
    gray = gray.rotate(angle, resample=Image.BILINEAR, expand=False, fillcolor=255)
    if level.downscale < 1.0:
        w, h = gray.size
        small = (max(1, int(w * level.downscale)), max(1, int(h * level.downscale)))
        gray = gray.resize(small, Image.BILINEAR).resize((w, h), Image.BILINEAR)
    arr = np.asarray(gray, dtype=np.float64)
    arr += nprng.normal(0.0, level.noise_sigma, arr.shape)
    gray = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    if level.blur_radius:
        gray = gray.filter(ImageFilter.GaussianBlur(level.blur_radius))
    buf = io.BytesIO()
    gray.save(buf, "JPEG", quality=level.jpeg_quality)
    return buf.getvalue()


def _build_pdf(spec: DocumentSpec, jpegs: list[bytes]) -> bytes:
    out = pymupdf.open()
    for jpeg in jpegs:
        page = out.new_page(width=612, height=792)  # letter, matches source
        page.insert_image(page.rect, stream=jpeg)
    stamp = FIXED_DOC_DT.strftime("D:%Y%m%d%H%M%S")
    out.set_metadata(
        {
            "title": spec.title,
            "author": "Meridian Benefits Group",
            "producer": "corpusgen scanned_pdf",
            "creationDate": stamp,
            "modDate": stamp,
        }
    )
    data = out.tobytes(deflate=True)
    out.close()
    return data


def _ocr_clears_plantings(pdf_bytes: bytes, plantings: list[dict]) -> bool:
    """Validation's exact check: rasterize the FINAL pdf at the validator's
    DPI and fuzz-match every planting on its recorded page."""
    page_texts: dict[int, str] = {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        for plant in plantings:
            page = plant["location"]["page"]
            if page not in page_texts:
                pix = pdf[page - 1].get_pixmap(dpi=DPI)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                page_texts[page] = _collapse(pytesseract.image_to_string(img))
            score = fuzz.partial_ratio(_collapse(plant["value"]), page_texts[page])
            if score < FUZZ_THRESHOLD:
                return False
    return True


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        digital_path = Path(tmp) / "digital.pdf"
        plantings = digital_pdf.render(spec, digital_path)
        with pymupdf.open(digital_path) as pdf:
            page_images = []
            for page in pdf:
                pix = page.get_pixmap(dpi=DPI)
                page_images.append(
                    Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                )

    seed = _spec_seed(spec)
    pdf_bytes = b""
    for level in LEVELS:
        jpegs = [
            degrade_page(img, level, seed + page_no)
            for page_no, img in enumerate(page_images, start=1)
        ]
        pdf_bytes = _build_pdf(spec, jpegs)
        if _ocr_clears_plantings(pdf_bytes, plantings):
            break
        # else: fall through to the next (milder) level; FAINT always ships.
    path.write_bytes(pdf_bytes)

    for plant in plantings:
        plant["presentation"] = "image"
    return plantings
