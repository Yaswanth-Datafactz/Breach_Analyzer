"""Pydantic response models for passages (Handbook §6.2: Pydantic models
on every endpoint). `locator` is passed through verbatim -- the UI's
locator breadcrumb renders straight from it (docs/plan.md §4)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class PassageOut(BaseModel):
    id: UUID
    document_id: UUID
    seq: int
    kind: str
    locator: dict
    text: str
    ocr: bool
    page_image_sha: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PassagePageOut(BaseModel):
    items: list[PassageOut]
    total: int
    limit: int
    offset: int
