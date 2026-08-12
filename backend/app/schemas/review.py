"""Pydantic models for /api/v1/review (Handbook §6.2). `resolved` carries
the kind-shaped evidence payload services/review.py builds -- extraction
items resolve to the element card, er_pair items to BOTH mentions' evidence
sets side by side (docs/plan.md §7's Review page)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ReviewItemOut(BaseModel):
    id: UUID
    kind: str
    ref: dict
    reason: str | None
    priority: int
    status: str
    created_at: datetime
    resolved: dict = Field(default_factory=dict)


class ReviewItemPageOut(BaseModel):
    items: list[ReviewItemOut]
    total: int
    limit: int
    offset: int


class ReviewDecisionIn(BaseModel):
    """POST /review/items/{id}/decision body. `decision` is validated
    against the item's kind by the service (extraction: correct|attach|
    dismiss; er_pair: merge|keep_separate; flag_audit: confirm|override);
    `after` carries the kind-specific payload (e.g. {'validation_status':
    'format_only'} for correct, {'person_id': ...} for attach)."""

    decision: Literal[
        "correct", "attach", "dismiss", "merge", "keep_separate", "confirm", "override"
    ]
    reviewer: str = Field(min_length=1, max_length=200)
    notes: str | None = None
    after: dict | None = None


class ReviewDecisionOut(BaseModel):
    review_item_id: UUID
    decision: str
    status: str
    recomputed_person_ids: list[UUID] = []
    detail: dict = Field(default_factory=dict)
