"""Data access for `approval_requests` (UC2's repositories layer) -- the
human gates docs/plan.md §3 puts on bulk merges, final sign-off, and class
pauses. Flush-only writes; commit timing belongs to the caller (the
request_approval tool commits before the run parks, the decision endpoint
commits after recording the human's answer)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ApprovalRequest


class ApprovalRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, approval_id: uuid.UUID) -> ApprovalRequest | None:
        return self.db.get(ApprovalRequest, approval_id)

    def create(
        self, *, agent_run_id: uuid.UUID, action_type: str, payload: dict
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            agent_run_id=agent_run_id, action_type=action_type, payload=payload
        )
        self.db.add(approval)
        self.db.flush()
        return approval

    def list(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ApprovalRequest], int]:
        conditions = []
        if status:
            conditions.append(ApprovalRequest.status == status)
        total = (
            self.db.scalar(
                select(func.count()).select_from(ApprovalRequest).where(*conditions)
            )
            or 0
        )
        items = (
            self.db.execute(
                select(ApprovalRequest)
                .where(*conditions)
                .order_by(ApprovalRequest.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(items), total

    def decide(
        self, approval: ApprovalRequest, *, status: str, decided_by: str
    ) -> None:
        """`status` is 'approved' | 'rejected' (the schema CHECK enforces
        it); decided_at is set here, once, so the audit trail carries the
        moment the human answered."""
        approval.status = status
        approval.decided_by = decided_by
        approval.decided_at = datetime.now(timezone.utc)
        self.db.flush()
