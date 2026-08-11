"""Pipeline-side data access (UC2's repositories layer): run lifecycle
transitions, live counters, document status updates, and the startup
stale-job reaper's UPDATEs. Kept separate from the read-oriented
DocumentRepository/ProcessingRunRepository so the API's query surface and
the pipeline's write surface don't grow into each other.

Counter updates REPLACE run.counters with a fresh dict on every write --
in-place mutation of a JSONB column is invisible to SQLAlchemy's change
tracking, and a silently-unsaved counter is exactly the kind of "nothing
silently dropped" violation this project cannot afford.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import (
    EXTRACTION_JOB_TERMINAL_STATUSES,
    Document,
    ExtractionJob,
    ProcessingRun,
)


class ProcessingRepository:
    def __init__(self, db: Session):
        self.db = db

    # -- run lifecycle -----------------------------------------------------

    def mark_run_running(self, run: ProcessingRun) -> None:
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        self.db.flush()

    def mark_run_finished(self, run: ProcessingRun, *, status: str = "finished") -> None:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        self.db.flush()

    def set_counters(self, run: ProcessingRun, counters: dict) -> None:
        run.counters = dict(counters)  # fresh dict: JSONB change tracking (see module docstring)
        self.db.flush()

    # -- document transitions ----------------------------------------------

    def set_document_status(
        self,
        document: Document,
        status: str,
        *,
        page_count: int | None = None,
        is_image_based: bool | None = None,
        file_class: str | None = None,
    ) -> None:
        document.status = status
        if page_count is not None:
            document.page_count = page_count
        if is_image_based is not None:
            document.is_image_based = is_image_based
        if file_class is not None:
            document.file_class = file_class
        self.db.flush()

    # -- startup reaping (UC2 pattern) -------------------------------------

    def reap_stale_jobs(self) -> int:
        """Mark every non-terminal extraction_jobs row failed. Safe at
        startup by construction: a fresh process cannot own any live job,
        so any non-terminal row was orphaned by a process that died."""
        result = self.db.execute(
            update(ExtractionJob)
            .where(ExtractionJob.status.not_in(EXTRACTION_JOB_TERMINAL_STATUSES))
            .values(status="failed", error="reaped at startup: process died mid-job")
        )
        return result.rowcount or 0

    def reap_stale_runs(self) -> int:
        """Same reasoning one level up: the pipeline runs in-process
        (FastAPI background task, plan D2), so a 'running' processing_runs
        row at startup can only be an orphan of a dead process."""
        result = self.db.execute(
            update(ProcessingRun)
            .where(ProcessingRun.status == "running")
            .values(status="failed", finished_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    # -- lookups the pipeline needs mid-run --------------------------------

    def get_document(self, document_id: uuid.UUID) -> Document | None:
        return self.db.get(Document, document_id)

    def get_run(self, run_id: uuid.UUID) -> ProcessingRun | None:
        return self.db.get(ProcessingRun, run_id)
