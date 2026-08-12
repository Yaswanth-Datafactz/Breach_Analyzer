"""Pydantic response models for /api/v1/exposure and /api/v1/persons
(Handbook §6.2: Pydantic models on every endpoint). Shapes mirror the
plan's §7 pages: the exposure listing row, and the person detail with
flags -> evidence drill-down plus the ER panel."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class AliasOut(BaseModel):
    name: str
    kind: str  # nickname|maiden|initials|misspelling|order_variant


class FlagOut(BaseModel):
    id: UUID
    category: str
    exposed: bool
    confidence: float | None
    review_status: str
    evidence_count: int = 0


class PersonSummaryOut(BaseModel):
    id: UUID
    run_id: UUID
    best_name: str
    aliases: list[AliasOut] = []
    dob: date | None
    er_confidence: float | None
    review_status: str
    mention_count: int
    document_count: int
    flags: list[FlagOut] = []


class ExposurePageOut(BaseModel):
    items: list[PersonSummaryOut]
    total: int
    limit: int
    offset: int
    run_id: UUID


class EvidenceOut(BaseModel):
    """One flag-evidence reference: everything the EvidenceDrawer needs to
    open the exact passage with the value highlighted (docs/plan.md §7)."""

    id: UUID
    pii_element_id: UUID
    element_type: str
    document_id: UUID
    document_filename: str | None
    passage_id: UUID
    snippet: str | None
    char_start: int | None
    char_end: int | None
    detector: str | None
    validation_status: str | None


class FlagDetailOut(FlagOut):
    evidence: list[EvidenceOut] = []


class IdentityLinkOut(BaseModel):
    """ER panel row (docs/plan.md §7: per-link rationale)."""

    id: UUID
    mention_id: UUID
    mention_name_raw: str | None
    document_filename: str | None
    score: float | None
    method: str
    rule_id: str | None
    rationale: str | None
    active: bool
    created_at: datetime


class PersonDetailOut(BaseModel):
    id: UUID
    run_id: UUID
    best_name: str
    aliases: list[AliasOut] = []
    dob: date | None
    er_confidence: float | None
    review_status: str
    mention_count: int
    document_count: int
    flags: list[FlagDetailOut] = []
    identity_links: list[IdentityLinkOut] = []
