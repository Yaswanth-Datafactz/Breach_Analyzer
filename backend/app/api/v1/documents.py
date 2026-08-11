"""/documents and /passages (docs/plan.md §5's API surface).

The router carries no prefix -- it hosts both the /documents/* routes and
GET /passages/{id} (the plan puts them in the same resource group: a
passage is only ever reached FROM a document drill-down, and splitting the
file would split that story). main.py includes this router once under
/api/v1, unchanged.

GET /documents/{id}/file serves the ORIGINAL bytes from the content-
addressed store -- the only way stored originals (including quarantined
ones) are ever reachable, and only behind the API key (docs/plan.md §11).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.documents import DocumentRepository
from app.repositories.passages import PassageRepository
from app.schemas.document import DocumentOut, DocumentPageOut
from app.schemas.errors import ErrorEnvelope
from app.schemas.passage import PassageOut, PassagePageOut
from app.services.parsing import storage

router = APIRouter(
    tags=["documents"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


@router.get("/documents", response_model=DocumentPageOut)
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


@router.get(
    "/documents/{document_id}",
    response_model=DocumentOut,
    responses={404: {"description": "Document not found", "model": ErrorEnvelope}},
)
def get_document(document_id: uuid.UUID, db: Session = Depends(get_db)) -> DocumentOut:
    document = DocumentRepository(db).get(document_id)
    if document is None:
        raise NotFoundError(f"Document {document_id} not found")
    return DocumentOut.model_validate(document)


@router.get(
    "/documents/{document_id}/passages",
    response_model=PassagePageOut,
    responses={404: {"description": "Document not found", "model": ErrorEnvelope}},
)
def list_document_passages(
    document_id: uuid.UUID,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> PassagePageOut:
    if DocumentRepository(db).get(document_id) is None:
        raise NotFoundError(f"Document {document_id} not found")
    items, total = PassageRepository(db).list_for_document(document_id, limit=limit, offset=offset)
    return PassagePageOut(
        items=[PassageOut.model_validate(p) for p in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/documents/{document_id}/file",
    response_class=FileResponse,
    responses={404: {"description": "Document or stored original not found", "model": ErrorEnvelope}},
)
def get_document_file(document_id: uuid.UUID, db: Session = Depends(get_db)) -> FileResponse:
    document = DocumentRepository(db).get(document_id)
    if document is None:
        raise NotFoundError(f"Document {document_id} not found")

    path = storage.original_path(document.sha256, Path(document.original_filename).suffix)
    if not path.exists():
        raise NotFoundError(
            f"Original bytes for document {document_id} are not in the content-addressed store"
        )

    # sniffed_mime may carry a `; reclassified-from=...` parameter note
    # (services/ingestion/classify.py) -- the bare type is the servable part.
    media_type = (document.sniffed_mime or "application/octet-stream").split(";")[0].strip()
    return FileResponse(path, media_type=media_type, filename=document.original_filename)


@router.get(
    "/passages/{passage_id}",
    response_model=PassageOut,
    tags=["passages"],
    responses={404: {"description": "Passage not found", "model": ErrorEnvelope}},
)
def get_passage(passage_id: uuid.UUID, db: Session = Depends(get_db)) -> PassageOut:
    passage = PassageRepository(db).get(passage_id)
    if passage is None:
        raise NotFoundError(f"Passage {passage_id} not found")
    return PassageOut.model_validate(passage)
