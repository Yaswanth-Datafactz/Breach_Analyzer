"""Pydantic response models for /api/v1/runs (Handbook §6.2: Pydantic
models on every endpoint)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RunCreateIn(BaseModel):
    """POST /runs body: the corpus directory to process. Everything else a
    run needs (models, thresholds, concurrency) comes from config and is
    frozen into config_snapshot by the pipeline -- the caller picks the
    corpus, never the knobs (docs/plan.md §4: replayable from documents +
    config snapshot)."""

    corpus_path: str = Field(min_length=1, description="Directory containing the corpus to ingest")


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
