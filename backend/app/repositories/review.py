"""Data access for `review_items` + `review_decisions` (UC2's repositories
layer). `get_for_update` is the race-safety primitive (UC2 pattern): the
decision endpoint locks the item row, re-checks it is still open, and only
then projects the decision -- two reviewers deciding the same item
serialize on the row lock and the loser gets a 409, never a double-apply."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import ReviewDecision, ReviewItem


class ReviewRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, item_id: uuid.UUID) -> ReviewItem | None:
        return self.db.get(ReviewItem, item_id)

    def get_for_update(self, item_id: uuid.UUID) -> ReviewItem | None:
        """SELECT ... FOR UPDATE -- the caller owns the row until commit."""
        return self.db.scalar(
            select(ReviewItem).where(ReviewItem.id == item_id).with_for_update()
        )

    def create_item(
        self,
        *,
        kind: str,
        ref: dict,
        reason: str | None = None,
        priority: int = 0,
    ) -> ReviewItem:
        item = ReviewItem(kind=kind, ref=ref, reason=reason, priority=priority)
        self.db.add(item)
        self.db.flush()  # populate item.id without committing yet
        return item

    def find_item_for_element(self, pii_element_id: uuid.UUID) -> ReviewItem | None:
        """Any-status lookup: an element already parked (or already decided)
        must not be parked again on recompute."""
        return self.db.scalar(
            select(ReviewItem).where(
                ReviewItem.kind == "extraction",
                ReviewItem.ref["pii_element_id"].astext == str(pii_element_id),
            )
        )

    def list_items(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ReviewItem], int]:
        query = select(ReviewItem)
        count_query = select(func.count()).select_from(ReviewItem)
        conditions = []
        if kind:
            conditions.append(ReviewItem.kind == kind)
        if status:
            conditions.append(ReviewItem.status == status)
        if run_id is not None:
            conditions.append(ReviewItem.ref["run_id"].astext == str(run_id))
        if conditions:
            query = query.where(*conditions)
            count_query = count_query.where(*conditions)
        total = self.db.scalar(count_query) or 0
        rows = self.db.scalars(
            query.order_by(
                ReviewItem.priority.desc(), ReviewItem.created_at.asc(), ReviewItem.id.asc()
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), total

    def delete_open_er_items_for_run(self, run_id: uuid.UUID) -> int:
        """An ER stage re-run invalidates its own open gray-band queue
        (the pairs will be re-emitted against fresh person ids). Decided
        items are audit history and stay."""
        result = self.db.execute(
            delete(ReviewItem).where(
                ReviewItem.kind == "er_pair",
                ReviewItem.status == "open",
                ReviewItem.ref["run_id"].astext == str(run_id),
            )
        )
        return result.rowcount or 0

    def create_decision(
        self,
        *,
        review_item_id: uuid.UUID,
        decision: str,
        reviewer: str,
        before: dict | None = None,
        after: dict | None = None,
        notes: str | None = None,
    ) -> ReviewDecision:
        row = ReviewDecision(
            review_item_id=review_item_id,
            decision=decision,
            before=before,
            after=after,
            reviewer=reviewer,
            notes=notes,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def decisions_for_item(self, item_id: uuid.UUID) -> list[ReviewDecision]:
        return list(
            self.db.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.review_item_id == item_id)
                .order_by(ReviewDecision.created_at.asc())
            )
        )
