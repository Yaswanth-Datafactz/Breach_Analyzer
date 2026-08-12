"""Data access for `cost_events` (UC2's repositories layer). Writes come
through services/extraction/costing.py's shared recorder; the read side
serves GET /costs/summary and /costs/extrapolation with real aggregate
queries over this table -- the §9 rule that every reported number is
traceable to cost_events rows, never re-derived from provider dashboards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CostEvent, Document


@dataclass(frozen=True)
class CostSummaryRow:
    purpose: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class ClassCostRow:
    file_class: str
    documents: int  # ALL run documents of the class (zero-LLM-cost docs count)
    documents_with_costs: int
    cost_usd: float


class CostEventRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        run_id: uuid.UUID | None,
        purpose: str,
        model: str,
        document_id: uuid.UUID | None = None,
        agent_run_id: uuid.UUID | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> CostEvent:
        event = CostEvent(
            run_id=run_id,
            purpose=purpose,
            model=model,
            document_id=document_id,
            agent_run_id=agent_run_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cost_usd=cost_usd,
        )
        self.db.add(event)
        self.db.flush()  # populate event.id without committing yet
        return event

    def has_events_for_run(self, run_id: uuid.UUID) -> bool:
        return (
            self.db.scalar(select(CostEvent.id).where(CostEvent.run_id == run_id).limit(1))
            is not None
        )

    def summary_for_run(self, run_id: uuid.UUID) -> list[CostSummaryRow]:
        rows = self.db.execute(
            select(
                CostEvent.purpose,
                CostEvent.model,
                func.count(CostEvent.id),
                func.coalesce(func.sum(CostEvent.input_tokens), 0),
                func.coalesce(func.sum(CostEvent.output_tokens), 0),
                func.coalesce(func.sum(CostEvent.cached_input_tokens), 0),
                func.coalesce(func.sum(CostEvent.cost_usd), 0),
            )
            .where(CostEvent.run_id == run_id)
            .group_by(CostEvent.purpose, CostEvent.model)
            .order_by(CostEvent.purpose, CostEvent.model)
        ).all()
        return [
            CostSummaryRow(
                purpose=purpose,
                model=model,
                calls=calls,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cached_input_tokens=int(cached),
                cost_usd=float(cost),
            )
            for purpose, model, calls, input_tokens, output_tokens, cached, cost in rows
        ]

    def costs_by_file_class(self, run_id: uuid.UUID) -> list[ClassCostRow]:
        """Per file_class: how many documents the run holds and what their
        document-attributed LLM calls cost (§9 extrapolation methodology:
        measured mean cost/doc per file_class x class mix). Documents with
        zero LLM cost are counted -- their $0 is a measurement, not a gap;
        eval/agent events without a document_id are excluded here and
        reported separately by the API layer."""
        doc_counts = dict(
            self.db.execute(
                select(Document.file_class, func.count(Document.id))
                .where(Document.run_id == run_id)
                .group_by(Document.file_class)
            ).all()
        )
        cost_rows = self.db.execute(
            select(
                Document.file_class,
                func.count(func.distinct(CostEvent.document_id)),
                func.coalesce(func.sum(CostEvent.cost_usd), 0),
            )
            .join(Document, Document.id == CostEvent.document_id)
            .where(CostEvent.run_id == run_id, CostEvent.document_id.is_not(None))
            .group_by(Document.file_class)
        ).all()
        costs = {file_class: (int(docs), float(cost)) for file_class, docs, cost in cost_rows}
        return [
            ClassCostRow(
                file_class=file_class,
                documents=int(total),
                documents_with_costs=costs.get(file_class, (0, 0.0))[0],
                cost_usd=costs.get(file_class, (0, 0.0))[1],
            )
            for file_class, total in sorted(doc_counts.items())
        ]

    def non_document_cost_for_run(self, run_id: uuid.UUID) -> float:
        """Cost of run events not attributable to a single document (agent
        checkpoints, eval) -- excluded from per-doc extrapolation and named
        instead of silently folded in."""
        value = self.db.scalar(
            select(func.coalesce(func.sum(CostEvent.cost_usd), 0)).where(
                CostEvent.run_id == run_id, CostEvent.document_id.is_(None)
            )
        )
        return float(value or 0)
