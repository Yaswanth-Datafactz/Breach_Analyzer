"""GET /costs/summary, GET /costs/extrapolation?scale= (docs/plan.md §5's
API surface; §9's methodology). Both endpoints are REAL aggregate queries
over cost_events -- a run with no cost rows is a 422, never a table of
invented zeros (§9: every reported number traceable to cost_events)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.security import require_api_key
from app.db.session import get_db
from app.repositories.cost_events import CostEventRepository
from app.repositories.runs import ProcessingRunRepository
from app.schemas.cost import (
    ClassExtrapolationRowOut,
    CostExtrapolationOut,
    CostSummaryOut,
    CostSummaryRowOut,
    CostTotalsOut,
)
from app.schemas.errors import ErrorEnvelope

router = APIRouter(
    prefix="/costs",
    tags=["costs"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


def _require_run(db: Session, run_id: uuid.UUID) -> None:
    if ProcessingRunRepository(db).get(run_id) is None:
        raise NotFoundError(f"Run {run_id} not found")


@router.get(
    "/summary",
    response_model=CostSummaryOut,
    responses={
        404: {"description": "Run not found", "model": ErrorEnvelope},
        422: {"description": "Run has no cost events", "model": ErrorEnvelope},
    },
)
def cost_summary(run_id: uuid.UUID, db: Session = Depends(get_db)) -> CostSummaryOut:
    """Per purpose/model: calls, tokens (incl. cached), USD, cache hit
    rates -- straight GROUP BY over cost_events."""
    _require_run(db, run_id)
    rows = CostEventRepository(db).summary_for_run(run_id)
    if not rows:
        raise ValidationFailedError(
            "Run has no cost events yet -- nothing to summarize",
            details={"run_id": str(run_id)},
        )
    out_rows = [
        CostSummaryRowOut.build(
            purpose=row.purpose,
            model=row.model,
            calls=row.calls,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cached_input_tokens=row.cached_input_tokens,
            cost_usd=row.cost_usd,
        )
        for row in rows
    ]
    total_input = sum(r.input_tokens for r in out_rows)
    total_cached = sum(r.cached_input_tokens for r in out_rows)
    totals = CostTotalsOut(
        calls=sum(r.calls for r in out_rows),
        input_tokens=total_input,
        output_tokens=sum(r.output_tokens for r in out_rows),
        cached_input_tokens=total_cached,
        cache_hit_rate=round(total_cached / total_input, 4) if total_input else 0.0,
        cost_usd=round(sum(r.cost_usd for r in out_rows), 6),
    )
    return CostSummaryOut(run_id=run_id, rows=out_rows, totals=totals)


@router.get(
    "/extrapolation",
    response_model=CostExtrapolationOut,
    responses={
        404: {"description": "Run not found", "model": ErrorEnvelope},
        422: {"description": "Run has no document-attributed cost events", "model": ErrorEnvelope},
    },
)
def cost_extrapolation(
    run_id: uuid.UUID,
    scale: int = Query(ge=1, le=1_000_000_000, description="Target corpus size in documents"),
    db: Session = Depends(get_db),
) -> CostExtrapolationOut:
    """§9 methodology: measured mean cost/doc per file_class x this run's
    class mix, scaled -- plus a class-mix sensitivity scenario, because the
    headline number inherits the corpus's composition."""
    _require_run(db, run_id)
    repo = CostEventRepository(db)
    class_rows = repo.costs_by_file_class(run_id)
    total_docs = sum(row.documents for row in class_rows)
    total_doc_cost = sum(row.cost_usd for row in class_rows)
    if total_docs == 0 or not any(row.documents_with_costs for row in class_rows):
        raise ValidationFailedError(
            "Run has no document-attributed cost events -- nothing to extrapolate from",
            details={"run_id": str(run_id)},
        )

    by_class: list[ClassExtrapolationRowOut] = []
    for row in class_rows:
        share = row.documents / total_docs
        mean = row.cost_usd / row.documents if row.documents else 0.0
        by_class.append(
            ClassExtrapolationRowOut(
                file_class=row.file_class,
                documents=row.documents,
                class_share=round(share, 4),
                measured_cost_usd=round(row.cost_usd, 6),
                mean_cost_per_doc_usd=round(mean, 8),
                cost_at_scale_usd=round(scale * share * mean, 4),
            )
        )

    mean_per_doc = total_doc_cost / total_docs
    extrapolated = scale * mean_per_doc

    # Sensitivity: +10pp of the mix into the most expensive class (by mean
    # cost/doc), everything else scaled down proportionally.
    shifted_class: str | None = None
    shifted_cost: float | None = None
    priced = [row for row in by_class if row.documents > 0]
    if len(priced) > 1:
        expensive = max(priced, key=lambda row: row.mean_cost_per_doc_usd)
        shift = min(0.10, 1.0 - expensive.class_share)
        remainder_share = 1.0 - expensive.class_share
        shifted_mean = 0.0
        for row in priced:
            if row.file_class == expensive.file_class:
                new_share = row.class_share + shift
            else:
                new_share = row.class_share * (1.0 - (expensive.class_share + shift)) / remainder_share
            shifted_mean += new_share * row.mean_cost_per_doc_usd
        shifted_class = expensive.file_class
        shifted_cost = round(scale * shifted_mean, 4)

    non_document_cost = repo.non_document_cost_for_run(run_id)
    return CostExtrapolationOut(
        run_id=run_id,
        scale=scale,
        measured_documents=total_docs,
        measured_document_cost_usd=round(total_doc_cost, 6),
        measured_non_document_cost_usd=round(non_document_cost, 6),
        mean_cost_per_doc_usd=round(mean_per_doc, 8),
        extrapolated_cost_usd=round(extrapolated, 4),
        by_class=by_class,
        sensitivity_shifted_class=shifted_class,
        sensitivity_extrapolated_cost_usd=shifted_cost,
        sensitivity_note=(
            "Extrapolation assumes this run's file-class mix; per-class means are "
            "provided so any target mix can be re-priced. The sensitivity figure "
            "re-prices the same means with 10 percentage points of the mix shifted "
            "into the most expensive class. Agent/eval costs (non-document) are "
            "reported separately and scale with exceptions, not documents (§9 "
            "sublinearity caveat); DeepSeek-side cache savings are not assumed (D9)."
        ),
    )
