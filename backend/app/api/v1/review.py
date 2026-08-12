"""GET /review/items?kind=&status=, POST /review/items/{id}/decision
(docs/plan.md §5's API surface; §7's Review page).

Listing returns items WITH resolved refs (services/review.py): extraction
items carry the element card, er_pair items carry both mentions' evidence
sets side by side. Decisions apply atomically and project back (§4):
extraction corrections re-run exposure for the affected person; er_pair
merge/keep-separate writes identity_links (append-only: flip + insert,
inactive row on split) plus a method='reviewer' er_decisions row, then
recomputes exposure for every affected person. Race safety is the UC2
row-lock pattern -- a concurrent duplicate decision gets 409, never a
double-apply."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.security import require_api_key
from app.db.session import get_db
from app.schemas.errors import ErrorEnvelope
from app.schemas.review import (
    ReviewDecisionIn,
    ReviewDecisionOut,
    ReviewItemOut,
    ReviewItemPageOut,
)
from app.services.review import decide_review_item, list_items_with_refs

router = APIRouter(
    prefix="/review",
    tags=["review"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


@router.get("/items", response_model=ReviewItemPageOut)
def list_review_items(
    kind: str | None = Query(default=None, pattern="^(extraction|er_pair|flag_audit)$"),
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(open|decided|dismissed)$"
    ),
    run_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ReviewItemPageOut:
    rows, total = list_items_with_refs(
        db, kind=kind, status=status_filter, run_id=run_id, limit=limit, offset=offset
    )
    items = [
        ReviewItemOut(
            id=item.id,
            kind=item.kind,
            ref=item.ref,
            reason=item.reason,
            priority=item.priority,
            status=item.status,
            created_at=item.created_at,
            resolved=resolved,
        )
        for item, resolved in rows
    ]
    return ReviewItemPageOut(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/items/{item_id}/decision",
    response_model=ReviewDecisionOut,
    responses={
        404: {"description": "Item (or its referent) not found", "model": ErrorEnvelope},
        409: {"description": "Item already decided", "model": ErrorEnvelope},
        422: {"description": "Decision invalid for this item kind", "model": ErrorEnvelope},
    },
)
def decide_item(
    item_id: uuid.UUID,
    body: ReviewDecisionIn,
    db: Session = Depends(get_db),
) -> ReviewDecisionOut:
    outcome = decide_review_item(
        db,
        item_id,
        decision=body.decision,
        reviewer=body.reviewer,
        notes=body.notes,
        after=body.after,
    )
    db.commit()
    return ReviewDecisionOut(
        review_item_id=outcome.item_id,
        decision=outcome.decision,
        status="decided",
        recomputed_person_ids=outcome.recomputed_person_ids,
        detail=outcome.detail,
    )
