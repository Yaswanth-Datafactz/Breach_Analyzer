"""Data access for `passages` (UC2's repositories layer)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Passage


class PassageRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, passage_id: uuid.UUID) -> Passage | None:
        return self.db.get(Passage, passage_id)

    def create(
        self,
        *,
        document_id: uuid.UUID,
        seq: int,
        kind: str,
        locator: dict,
        text: str,
        ocr: bool = False,
        page_image_sha: str | None = None,
    ) -> Passage:
        passage = Passage(
            document_id=document_id,
            seq=seq,
            kind=kind,
            locator=locator,
            text=text,
            ocr=ocr,
            page_image_sha=page_image_sha,
        )
        self.db.add(passage)
        self.db.flush()  # populate passage.id without committing yet
        return passage

    def list_for_document(
        self, document_id: uuid.UUID, *, limit: int = 200, offset: int = 0
    ) -> tuple[list[Passage], int]:
        base = select(Passage).where(Passage.document_id == document_id)
        total = (
            self.db.scalar(
                select(func.count()).select_from(Passage).where(Passage.document_id == document_id)
            )
            or 0
        )
        rows = self.db.scalars(base.order_by(Passage.seq).limit(limit).offset(offset)).all()
        return list(rows), total
