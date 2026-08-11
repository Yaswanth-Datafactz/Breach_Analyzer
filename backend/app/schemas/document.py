"""Pydantic response models for /api/v1/documents (Handbook §6.2: Pydantic
models on every endpoint)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: UUID
    run_id: UUID
    original_filename: str
    rel_path: str
    declared_mime: str | None
    sniffed_mime: str | None
    byte_size: int
    file_class: str
    source_kind: str
    parent_document_id: UUID | None
    status: str
    page_count: int | None
    is_image_based: bool | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentPageOut(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int
