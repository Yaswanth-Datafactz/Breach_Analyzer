"""Data access for `documents` (UC2's repositories layer)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Document


class DocumentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, document_id: uuid.UUID) -> Document | None:
        return self.db.get(Document, document_id)

    def get_by_sha256(self, sha256: str, *, run_id: uuid.UUID) -> Document | None:
        """Dedup lookup, scoped to one run (migration 002 / docs/plan.md
        §14b R2: sha256 is UNIQUE per run, not globally -- an ingest must
        only dedup against its OWN run's rows, never another run's)."""
        return self.db.scalar(
            select(Document).where(Document.sha256 == sha256, Document.run_id == run_id)
        )

    def create(
        self,
        *,
        run_id: uuid.UUID,
        sha256: str,
        original_filename: str,
        rel_path: str,
        byte_size: int,
        declared_mime: str | None = None,
        sniffed_mime: str | None = None,
        file_class: str = "unknown",
        source_kind: str = "corpus",
        parent_document_id: uuid.UUID | None = None,
    ) -> Document:
        document = Document(
            run_id=run_id,
            sha256=sha256,
            original_filename=original_filename,
            rel_path=rel_path,
            byte_size=byte_size,
            declared_mime=declared_mime,
            sniffed_mime=sniffed_mime,
            file_class=file_class,
            source_kind=source_kind,
            parent_document_id=parent_document_id,
            status="queued",
        )
        self.db.add(document)
        self.db.flush()  # populate document.id without committing yet
        return document

    def list(
        self,
        *,
        file_class: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Document], int]:
        query = select(Document)
        count_query = select(func.count()).select_from(Document)
        if file_class:
            query = query.where(Document.file_class == file_class)
            count_query = count_query.where(Document.file_class == file_class)
        if status:
            query = query.where(Document.status == status)
            count_query = count_query.where(Document.status == status)

        total = self.db.scalar(count_query) or 0
        rows = self.db.scalars(
            query.order_by(Document.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(rows), total
