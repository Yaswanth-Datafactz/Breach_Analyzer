"""GET /exports/exposure.csv|.xlsx (docs/plan.md §5's API surface): the
exposure table as a file, §2's column groups (identity | flags | evidence |
quality), one row per person. CSV streams row-by-row; XLSX is built
write-only and returned whole (the zip container cannot stream). Both
render from the same iterator (services/export.py) so they cannot drift."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.persons import PersonRepository
from app.schemas.errors import ErrorEnvelope
from app.services.export import build_exposure_xlsx, iter_exposure_csv

router = APIRouter(
    prefix="/exports",
    tags=["exports"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


def _resolve_run_id(db: Session, run_id: uuid.UUID | None) -> uuid.UUID:
    if run_id is not None:
        return run_id
    resolved = PersonRepository(db).latest_run_with_persons()
    if resolved is None:
        raise NotFoundError("No run has resolved persons yet -- run the ER stage first")
    return resolved


@router.get("/exposure.csv", response_class=StreamingResponse)
def export_exposure_csv(
    run_id: uuid.UUID | None = None, db: Session = Depends(get_db)
) -> StreamingResponse:
    resolved_run = _resolve_run_id(db, run_id)
    return StreamingResponse(
        iter_exposure_csv(db, resolved_run),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="exposure-{resolved_run}.csv"'
        },
    )


@router.get("/exposure.xlsx", response_class=Response)
def export_exposure_xlsx(
    run_id: uuid.UUID | None = None, db: Session = Depends(get_db)
) -> Response:
    resolved_run = _resolve_run_id(db, run_id)
    content = build_exposure_xlsx(db, resolved_run)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="exposure-{resolved_run}.xlsx"'
        },
    )
