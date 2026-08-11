"""Pydantic response models for /api/v1/runs (Handbook §6.2: Pydantic
models on every endpoint)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class RunOut(BaseModel):
    id: UUID
    status: str
    config_snapshot: dict
    counters: dict
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunPageOut(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int
