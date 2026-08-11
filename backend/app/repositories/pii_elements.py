"""Data access for `pii_elements` (UC2's repositories layer). B1 writes
tier-0 detector hits through here (services/pipeline.py owns that
persistence per the detectors module contract); B2's extraction service
adds the llm_tier1/llm_tier2 rows through the same repository."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.db.models import PiiElement


class PiiElementRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, element_id: uuid.UUID) -> PiiElement | None:
        return self.db.get(PiiElement, element_id)

    def create(
        self,
        *,
        document_id: uuid.UUID,
        passage_id: uuid.UUID,
        element_type: str,
        value_raw: str,
        value_normalized: str,
        char_start: int,
        char_end: int,
        detector: str,
        validation_status: str,
        context_snippet: str | None = None,
        mention_id: uuid.UUID | None = None,
        confidence: float | None = None,
        signals: dict | None = None,
    ) -> PiiElement:
        element = PiiElement(
            document_id=document_id,
            passage_id=passage_id,
            mention_id=mention_id,
            element_type=element_type,
            value_raw=value_raw,
            value_normalized=value_normalized,
            char_start=char_start,
            char_end=char_end,
            context_snippet=context_snippet,
            detector=detector,
            validation_status=validation_status,
            confidence=confidence,
            signals=signals,
        )
        self.db.add(element)
        self.db.flush()  # populate element.id without committing yet
        return element
