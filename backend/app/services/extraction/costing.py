"""The shared cost recorder (docs/plan.md §9: "Every call writes
cost_events"). One function every LLM call site goes through -- tier 1,
tier-1 repair, tier-2 text, tier-2 vision now; the agent layer later --
so the §4 invariant is structural: a call that happened but wrote no
cost_events row cannot exist, and the extrapolation tables never have to
guess.

Pricing comes from core.config.MODEL_PRICES_USD_PER_MTOK (per-MTok, keyed
by the exact string stored in cost_events.model). Cached input tokens are
billed at the model's cached_input rate when the table has one; models
without a published cache-read rate (DeepSeek via Foundry -- D9 measured
zero cache hits there anyway) bill cached tokens at the full input rate,
which can only ever OVERstate their cost -- estimates err against us,
never for us.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.config import MODEL_PRICES_USD_PER_MTOK
from app.core.logging import get_logger
from app.db.models import CostEvent
from app.repositories.cost_events import CostEventRepository
from app.services.extraction.base import ExtractionUsage

logger = get_logger("extraction.costing")


def estimate_cost_usd(model: str, usage: ExtractionUsage) -> float:
    """Price one call. Unknown model -> $0 with a loud log line: a wrong
    price would silently poison every extrapolation built on this table
    (plan §9: never quote unverified numbers), so absence is surfaced,
    not papered over with a guess."""
    prices = MODEL_PRICES_USD_PER_MTOK.get(model)
    if prices is None:
        logger.error("model_price_missing", model=model)
        return 0.0
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached = usage.input_tokens - cached
    cached_price = prices.get("cached_input", prices["input"])
    cost = (
        uncached * prices["input"]
        + cached * cached_price
        + usage.output_tokens * prices["output"]
    ) / 1_000_000
    return round(cost, 6)


def record_cost_event(
    db: Session,
    *,
    run_id: uuid.UUID | None,
    purpose: str,
    model: str,
    usage: ExtractionUsage,
    document_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> CostEvent:
    """Persist one cost_events row for one completed LLM call (§4 purpose
    vocabulary: tier1 | tier2_text | tier2_vision | agent_* | eval)."""
    return CostEventRepository(db).create(
        run_id=run_id,
        purpose=purpose,
        model=model,
        document_id=document_id,
        agent_run_id=agent_run_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cost_usd=estimate_cost_usd(model, usage),
    )
