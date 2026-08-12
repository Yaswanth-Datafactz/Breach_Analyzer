"""Budget arithmetic (docs/plan.md §3's AgentRunner mechanics: budgets --
steps/tokens/USD -- checked between turns; exceeding any dimension parks the
run as `budget_exceeded` with partial findings).

The Budget is immutable and lives beside the run's own rollup columns
(agent_runs.steps_used / tokens_in+tokens_out / cost_usd) -- the check reads
the persisted rollups, never a parallel in-memory counter, so a resumed or
crashed-and-inspected run budgets against exactly what the trace shows.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import MODEL_PRICES_USD_PER_MTOK, Settings
from app.db.models import AgentRun


@dataclass(frozen=True)
class Budget:
    max_steps: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None

    def exceeded_dimension(self, run: AgentRun) -> str | None:
        """First exhausted dimension, or None while headroom remains.
        Checked BETWEEN turns: a step that crosses a line completes (its
        trace is already persisted), the next turn never starts."""
        if self.max_steps is not None and run.steps_used >= self.max_steps:
            return "steps"
        if self.max_tokens is not None and (run.tokens_in + run.tokens_out) >= self.max_tokens:
            return "tokens"
        if self.max_usd is not None and float(run.cost_usd) >= self.max_usd:
            return "usd"
        return None


def budget_for_kind(kind: str, settings: Settings, *, unit_count: int = 1) -> Budget:
    """Config defaults per agent kind (docs/plan.md §3's table). Per-unit
    budgets scale with `unit_count`: the auditor's '3 steps/flag' becomes
    3 x sample size on its single run over the stratified sample."""
    if kind == "orchestrator":
        return Budget(
            max_steps=settings.orchestrator_max_steps,
            max_usd=settings.orchestrator_max_usd,
        )
    if kind == "investigator":
        return Budget(
            max_steps=settings.investigator_max_steps,
            max_tokens=settings.investigator_max_tokens,
            max_usd=settings.investigator_max_usd,
        )
    if kind == "adjudicator":
        return Budget(
            max_steps=settings.adjudicator_max_steps,
            max_usd=settings.adjudicator_max_usd,
        )
    if kind == "auditor":
        return Budget(
            max_steps=settings.auditor_max_steps_per_flag * max(1, unit_count),
            max_usd=settings.auditor_max_usd_per_run,
        )
    raise ValueError(f"unknown agent kind: {kind}")


def with_overrides(
    budget: Budget,
    *,
    max_steps: int | None = None,
    max_tokens: int | None = None,
    max_usd: float | None = None,
    force_failure: bool = False,
) -> Budget:
    """Manual-dispatch overrides (POST /agents/runs). `force_failure` is the
    scripted demo knob (docs/plan.md §3: the investigator vs the password-
    protected PDF at a 4-step budget) -- it CAPS max_steps at 4, it never
    raises a tighter override."""
    result = Budget(
        max_steps=max_steps if max_steps is not None else budget.max_steps,
        max_tokens=max_tokens if max_tokens is not None else budget.max_tokens,
        max_usd=max_usd if max_usd is not None else budget.max_usd,
    )
    if force_failure:
        capped = 4 if result.max_steps is None else min(result.max_steps, 4)
        result = Budget(
            max_steps=capped, max_tokens=result.max_tokens, max_usd=result.max_usd
        )
    return result


def step_cost_usd(
    model: str | None, tokens_in: int, tokens_out: int, cached_in: int = 0
) -> float:
    """Price one model turn from the MODEL_PRICES table (docs/plan.md §9:
    every call priced from the versioned table, never re-derived). Unknown
    models (a test double with a made-up name) price at 0 rather than
    crashing the loop mid-run."""
    prices = MODEL_PRICES_USD_PER_MTOK.get(model or "")
    if prices is None:
        return 0.0
    cost = (
        tokens_in * prices["input"]
        + tokens_out * prices["output"]
        + cached_in * prices.get("cached_input", prices["input"] * 0.1)
    ) / 1_000_000
    return round(cost, 6)
