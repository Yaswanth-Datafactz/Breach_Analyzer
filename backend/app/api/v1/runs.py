"""GET /runs (docs/plan.md §5's API surface). List is live now to prove the
router -> repository -> DB layering end to end; POST /runs, GET /runs/{id},
and GET /runs/{id}/quarantines land with the pipeline service in phase B1 --
no fake endpoints before the behavior behind them exists."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.runs import ProcessingRunRepository
from app.schemas.errors import ErrorEnvelope
from app.schemas.run import RunOut, RunPageOut

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
