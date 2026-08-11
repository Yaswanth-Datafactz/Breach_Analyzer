"""Data access for `processing_runs` (UC2's repositories layer -- no router
in this app calls `select()` directly; every query goes through a repository
like this one)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ProcessingRun


class ProcessingRunRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, run_id: uuid.UUID) -> ProcessingRun | None:
        return self.db.get(ProcessingRun, run_id)

    def create(self, *, config_snapshot: dict) -> ProcessingRun:
        run = ProcessingRun(config_snapshot=config_snapshot, status="created")
        self.db.add(run)
        self.db.flush()  # populate run.id without committing yet
        return run

    def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ProcessingRun], int]:
        query = select(ProcessingRun)
        count_query = select(func.count()).select_from(ProcessingRun)
        if status:
            query = query.where(ProcessingRun.status == status)
            count_query = count_query.where(ProcessingRun.status == status)

        total = self.db.scalar(count_query) or 0
        rows = self.db.scalars(
            query.order_by(ProcessingRun.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(rows), total
