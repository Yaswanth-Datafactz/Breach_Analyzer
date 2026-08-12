"""Data access for `er_decisions` (UC2's repositories layer): one row per
pair verdict -- rule-band decisions from the ER stage, adjudicator agent
decisions, and reviewer decisions all land here with the same shape, so
"why are these two mentions together (or apart)?" is always one query.

left_ref/right_ref carry {mention_id, run_id} (JSONB per docs/plan.md §4);
run_id inside the ref is what lets an ER stage re-run clear exactly its own
prior rule decisions without a schema change."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import ErDecision


class ErDecisionRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        left_ref: dict,
        right_ref: dict,
        decision: str,
        method: str,
        score: float | None = None,
        features: dict | None = None,
        rationale: str | None = None,
        agent_run_id: uuid.UUID | None = None,
        approval_request_id: uuid.UUID | None = None,
    ) -> ErDecision:
        row = ErDecision(
            left_ref=left_ref,
            right_ref=right_ref,
            decision=decision,
            score=score,
            features=features,
            method=method,
            rationale=rationale,
            agent_run_id=agent_run_id,
            approval_request_id=approval_request_id,
        )
        self.db.add(row)
        self.db.flush()  # populate row.id without committing yet
        return row

    def delete_rule_decisions_for_run(self, run_id: uuid.UUID) -> int:
        """Clear the deterministic ER stage's own prior output before a
        re-run. Agent/reviewer decisions are judgment records, not stage
        output -- they are never cleared here."""
        result = self.db.execute(
            delete(ErDecision).where(
                ErDecision.method == "rule",
                ErDecision.left_ref["run_id"].astext == str(run_id),
            )
        )
        return result.rowcount or 0

    def list_for_mention(self, mention_id: uuid.UUID) -> list[ErDecision]:
        mid = str(mention_id)
        return list(
            self.db.scalars(
                select(ErDecision)
                .where(
                    (ErDecision.left_ref["mention_id"].astext == mid)
                    | (ErDecision.right_ref["mention_id"].astext == mid)
                )
                .order_by(ErDecision.created_at.asc())
            )
        )
