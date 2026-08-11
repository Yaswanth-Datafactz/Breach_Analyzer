"""Pydantic response models for quarantines (Handbook §6.2). Document
filename/rel_path are denormalized into the response -- a quarantine row is
unreadable without the file it condemns, and the Agents/Review UIs list
these without a second fetch."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class QuarantineOut(BaseModel):
    id: UUID
    document_id: UUID
    document_filename: str
    document_rel_path: str
    stage: str
    reason_code: str
    detail: str | None
    status: str
    created_at: datetime


class QuarantinePageOut(BaseModel):
    items: list[QuarantineOut]
    total: int
    limit: int
    offset: int
