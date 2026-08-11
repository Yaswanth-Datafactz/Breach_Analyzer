"""Data access for `quarantines` (UC2's repositories layer). The listing
joins `documents` because a quarantine row is meaningless to a caller
without the file it condemns -- the API schema and the investigator agent's
queue both need filename/rel_path alongside the reason."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document, Quarantine


class QuarantineRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, quarantine_id: uuid.UUID) -> Quarantine | None:
        return self.db.get(Quarantine, quarantine_id)

    def create(
        self,
        *,
        document_id: uuid.UUID,
        stage: str,
        reason_code: str,
        detail: str | None = None,
    ) -> Quarantine:
        quarantine = Quarantine(
            document_id=document_id, stage=stage, reason_code=reason_code, detail=detail
        )
        self.db.add(quarantine)
        self.db.flush()  # populate quarantine.id without committing yet
        return quarantine

    def list_for_run(
        self, run_id: uuid.UUID, *, limit: int = 100, offset: int = 0
    ) -> tuple[list[tuple[Quarantine, Document]], int]:
        base = (
            select(Quarantine, Document)
            .join(Document, Quarantine.document_id == Document.id)
            .where(Document.run_id == run_id)
        )
        total = (
            self.db.scalar(
                select(func.count())
                .select_from(Quarantine)
                .join(Document, Quarantine.document_id == Document.id)
                .where(Document.run_id == run_id)
            )
            or 0
        )
        rows = self.db.execute(
            base.order_by(Quarantine.created_at).limit(limit).offset(offset)
        ).all()
        return [(row[0], row[1]) for row in rows], total
