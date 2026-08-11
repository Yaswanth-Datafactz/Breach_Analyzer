"""/runs (docs/plan.md §5's API surface): create + observe processing runs.

POST /runs starts the pipeline as a FastAPI background task (plan D2 /
Handbook §6.2: background jobs for long-running work) -- the response
returns immediately with the created run; GET /runs/{id} then serves the
live counters the pipeline commits after every document, so a poll loop
sees ingest/parse progress in real time."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.quarantines import QuarantineRepository
from app.repositories.runs import ProcessingRunRepository
from app.schemas.errors import ErrorEnvelope
from app.schemas.quarantine import QuarantineOut, QuarantinePageOut
from app.schemas.run import RunCreateIn, RunOut, RunPageOut
from app.services.pipeline import build_config_snapshot, run_processing_run

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


@router.get("", response_model=RunPageOut)
def list_runs(
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> RunPageOut:
    repo = ProcessingRunRepository(db)
    items, total = repo.list(status=status_filter, limit=limit, offset=offset)
    return RunPageOut(
        items=[RunOut.model_validate(r) for r in items], total=total, limit=limit, offset=offset
    )


@router.post(
    "",
    response_model=RunOut,
    status_code=status.HTTP_201_CREATED,
    responses={422: {"description": "corpus_path is not a directory", "model": ErrorEnvelope}},
)
def create_run(
    body: RunCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> RunOut:
    corpus_dir = Path(body.corpus_path).expanduser().resolve()
    if not corpus_dir.is_dir():
        raise ValidationFailedError(
            "corpus_path is not an existing directory",
            details={"corpus_path": str(corpus_dir)},
        )

    run = ProcessingRunRepository(db).create(
        config_snapshot=build_config_snapshot(corpus_path=str(corpus_dir))
    )
    db.commit()  # the background task reads the row from its own session

    background_tasks.add_task(run_processing_run, run.id)
    return RunOut.model_validate(run)


@router.get(
    "/{run_id}",
    response_model=RunOut,
    responses={404: {"description": "Run not found", "model": ErrorEnvelope}},
)
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> RunOut:
    run = ProcessingRunRepository(db).get(run_id)
    if run is None:
        raise NotFoundError(f"Run {run_id} not found")
    return RunOut.model_validate(run)


@router.get(
    "/{run_id}/quarantines",
    response_model=QuarantinePageOut,
    responses={404: {"description": "Run not found", "model": ErrorEnvelope}},
)
def list_run_quarantines(
    run_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> QuarantinePageOut:
    if ProcessingRunRepository(db).get(run_id) is None:
        raise NotFoundError(f"Run {run_id} not found")
    rows, total = QuarantineRepository(db).list_for_run(run_id, limit=limit, offset=offset)
    items = [
        QuarantineOut(
            id=quarantine.id,
            document_id=quarantine.document_id,
            document_filename=document.original_filename,
            document_rel_path=document.rel_path,
            stage=quarantine.stage,
            reason_code=quarantine.reason_code,
            detail=quarantine.detail,
            status=quarantine.status,
            created_at=quarantine.created_at,
        )
        for quarantine, document in rows
    ]
    return QuarantinePageOut(items=items, total=total, limit=limit, offset=offset)
