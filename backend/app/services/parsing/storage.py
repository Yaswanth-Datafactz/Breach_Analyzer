"""Content-addressed original-file storage (UC2's sha256-keyed store, copied
near-verbatim from Document_Extraction/backend/app/services/storage.py):
identical files -- e.g. the same attachment on five emails -- dedupe to the
same sha256-keyed path rather than duplicating bytes, which is exactly the
attachment-dedup saving docs/plan.md §9 measures. Quarantined originals live
here too, served only via the authenticated /documents/{id}/file route
(docs/plan.md §11).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.core.config import get_settings


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _original_path(sha256: str, suffix: str) -> Path:
    root = Path(get_settings().storage_root) / "originals"
    return root / sha256[0:2] / sha256[2:4] / f"{sha256}{suffix}"


def store_original(content: bytes, sha256: str, suffix: str) -> Path:
    """Writes `content` to its content-addressed path if not already
    present (re-ingesting an unchanged file is a no-op write), and
    returns the path."""
    path = _original_path(sha256, suffix)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return path


def original_path(sha256: str, suffix: str) -> Path:
    return _original_path(sha256, suffix)


def _page_image_path(sha256: str, page_no: int) -> Path:
    root = Path(get_settings().storage_root) / "pages"
    return root / sha256[0:2] / sha256[2:4] / f"{sha256}_{page_no}.png"


def page_image_path(sha256: str, page_no: int) -> Path:
    return _page_image_path(sha256, page_no)


def store_page_image(content: bytes, sha256: str, page_no: int) -> Path:
    """Caches a rasterized page PNG the first time it's produced (OCR path
    or the PassageViewer's drill-down) -- re-rendering the same page on every
    request would waste real CPU once the page exists."""
    path = _page_image_path(sha256, page_no)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return path
