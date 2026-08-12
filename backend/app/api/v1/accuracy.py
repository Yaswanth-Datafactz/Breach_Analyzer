"""POST /accuracy/runs, GET /accuracy/runs, GET /accuracy/runs/{id}
(docs/plan.md §5's API surface; §10's methodology).

POST kicks off scoring an EXISTING processing run against a manifest as a
background task (202 -- mirrors api/v1/agents.py's dispatch pattern: the
row is created synchronously so the response carries a real id, the
background task fills it in and never raises past its own boundary).
Prerequisite checks (run exists, run has persons/flags yet) happen
SYNCHRONOUSLY here, before dispatch -- same "never invented numbers"
discipline as api/v1/costs.py's `_require_run`: a run with nothing to
score is a 422 the caller sees immediately, not a background task that
silently produces an empty/misleading rollup.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.session import SessionLocal, get_db
from app.repositories.accuracy import AccuracyRunRepository
from app.repositories.persons import PersonRepository
from app.repositories.runs import ProcessingRunRepository
from app.schemas.accuracy import (
    AccuracyRunCreateIn,
    AccuracyRunOut,
    AccuracyRunPageOut,
    AccuracyRunSummaryOut,
)
from app.schemas.errors import ErrorEnvelope
from app.services import accuracy as accuracy_service

logger = get_logger("api.accuracy")

router = APIRouter(
    prefix="/accuracy",
    tags=["accuracy"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


def _execute_accuracy_scoring_task(accuracy_run_id: uuid.UUID) -> None:
    """Background task body: its own session, never raises (a failed
    scoring attempt is recorded on the row -- see execute_accuracy_
    scoring's docstring; this wrapper only guards the truly unexpected,
    e.g. the DB itself going away mid-task)."""
    db = SessionLocal()
    try:
        accuracy_service.execute_accuracy_scoring(db, accuracy_run_id)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("accuracy_dispatch_crashed", accuracy_run_id=str(accuracy_run_id))
    finally:
        db.close()


@router.post(
    "/runs",
    response_model=AccuracyRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "Processing run not found", "model": ErrorEnvelope},
        422: {
            "description": "Processing run has no persons/flags yet, or manifest_path does not exist",
            "model": ErrorEnvelope,
        },
    },
)
def create_accuracy_run(
    body: AccuracyRunCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> AccuracyRunOut:
    processing_run = ProcessingRunRepository(db).get(body.processing_run_id)
    if processing_run is None:
        raise NotFoundError(f"Processing run {body.processing_run_id} not found")

    if not PersonRepository(db).iter_for_run(body.processing_run_id):
        raise ValidationFailedError(
            "Processing run has no persons/flags yet -- nothing to score",
            details={"processing_run_id": str(body.processing_run_id)},
        )

    resolved_manifest_path = accuracy_service.resolve_manifest_path(body.manifest_path)
    if not resolved_manifest_path.is_file():
        raise ValidationFailedError(
            "manifest_path does not exist",
            details={"manifest_path": body.manifest_path, "resolved": str(resolved_manifest_path)},
        )

    accuracy_run = accuracy_service.create_accuracy_run(
        db,
        processing_run_id=body.processing_run_id,
        manifest_path=body.manifest_path,
        config_profile=body.config_profile,
    )
    db.commit()  # the background task reads the row from its own session

    background_tasks.add_task(_execute_accuracy_scoring_task, accuracy_run.id)
    return AccuracyRunOut.build(accuracy_run)


@router.get("/runs", response_model=AccuracyRunPageOut)
def list_accuracy_runs(
    limit: int = 50, offset: int = 0, db: Session = Depends(get_db)
) -> AccuracyRunPageOut:
    items, total = AccuracyRunRepository(db).list(limit=limit, offset=offset)
    return AccuracyRunPageOut(
        items=[AccuracyRunSummaryOut.build(r) for r in items], total=total, limit=limit, offset=offset
    )


@router.get(
    "/runs/{accuracy_run_id}",
    response_model=AccuracyRunOut,
    responses={404: {"description": "Accuracy run not found", "model": ErrorEnvelope}},
)
def get_accuracy_run(accuracy_run_id: uuid.UUID, db: Session = Depends(get_db)) -> AccuracyRunOut:
    accuracy_run = AccuracyRunRepository(db).get(accuracy_run_id)
    if accuracy_run is None:
        raise NotFoundError(f"Accuracy run {accuracy_run_id} not found")
    return AccuracyRunOut.build(accuracy_run)
