"""Pydantic response models for /api/v1/costs (Handbook §6.2: Pydantic
models on every endpoint). Every number these carry is an aggregate over
cost_events rows -- §9's rule that reported numbers trace to the table,
never to provider dashboards, and never get invented (an empty run is a
422, not a zero)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


def _tier_for_purpose(purpose: str) -> str:
    if purpose == "tier1":
        return "tier1"
    if purpose.startswith("tier2"):
        return "tier2"
    if purpose.startswith("agent_"):
        return "agents"
    return purpose  # 'eval'


class CostSummaryRowOut(BaseModel):
    purpose: str
    tier: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_hit_rate: float  # cached / total input tokens for this row
    cost_usd: float

    @classmethod
    def build(
        cls,
        *,
        purpose: str,
        model: str,
        calls: int,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        cost_usd: float,
    ) -> "CostSummaryRowOut":
        return cls(
            purpose=purpose,
            tier=_tier_for_purpose(purpose),
            model=model,
            calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_hit_rate=round(cached_input_tokens / input_tokens, 4) if input_tokens else 0.0,
            cost_usd=round(cost_usd, 6),
        )


class CostTotalsOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_hit_rate: float
    cost_usd: float


class CostSummaryOut(BaseModel):
    run_id: UUID
    rows: list[CostSummaryRowOut]
    totals: CostTotalsOut


class ClassExtrapolationRowOut(BaseModel):
    file_class: str
    documents: int
    class_share: float  # this run's class mix (documents / total documents)
    measured_cost_usd: float
    mean_cost_per_doc_usd: float
    cost_at_scale_usd: float  # scale * class_share * mean_cost_per_doc


class CostExtrapolationOut(BaseModel):
    run_id: UUID
    scale: int
    measured_documents: int
    measured_document_cost_usd: float
    # Run cost not attributable to any single document (agents/eval) --
    # named and excluded from the per-doc model rather than folded in.
    measured_non_document_cost_usd: float
    mean_cost_per_doc_usd: float
    extrapolated_cost_usd: float
    by_class: list[ClassExtrapolationRowOut]
    # Class-mix sensitivity (§9): the same means under a mix where the most
    # expensive class's share grows by 10 percentage points (others scaled
    # down proportionally) -- how exposed the headline number is to the
    # corpus's class composition.
    sensitivity_shifted_class: str | None
    sensitivity_extrapolated_cost_usd: float | None
    sensitivity_note: str
