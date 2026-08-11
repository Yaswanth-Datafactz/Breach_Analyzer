"""GET /documents (docs/plan.md §5's API surface). List with the plan's
?class/?status filters is live now; GET /documents/{id}[/passages|/file]
lands with the ingest/parse services in phase B1 -- the /file route in
particular waits for the content-addressed store to hold real originals
(docs/plan.md §11: served only via this authenticated route)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.documents import DocumentRepository
from app.schemas.document import DocumentOut, DocumentPageOut
from app.schemas.errors import ErrorEnvelope

router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


@router.get("", response_model=DocumentPageOut)
def list_documents(
    file_class: str | None = Query(default=None, alias="class"),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> DocumentPageOut:
    repo = DocumentRepository(db)
    items, total = repo.list(file_class=file_class, status=status_filter, limit=limit, offset=offset)
    return DocumentPageOut(
        items=[DocumentOut.model_validate(d) for d in items], total=total, limit=limit, offset=offset
    )
