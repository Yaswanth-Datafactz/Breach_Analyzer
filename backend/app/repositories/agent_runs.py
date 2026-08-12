"""Data access for `agent_runs` / `agent_steps` / `agent_tool_calls` (UC2's
repositories layer). The write methods flush (never commit) -- COMMIT TIMING
IS THE RUNNER'S CONTRACT: docs/plan.md §3 requires every step and tool call
persisted BEFORE execution continues, so services/agents/traces.py commits at
exactly those points and this repository stays policy-free."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AgentRun, AgentStep, AgentToolCall


class AgentRunRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, agent_run_id: uuid.UUID) -> AgentRun | None:
        return self.db.get(AgentRun, agent_run_id)

    def get_with_trace(self, agent_run_id: uuid.UUID) -> AgentRun | None:
        """Run + steps + tool calls in two queries (selectinload), ordered
        by step_no -- the GET /agents/runs/{id} trace payload."""
        return self.db.execute(
            select(AgentRun)
            .options(selectinload(AgentRun.steps).selectinload(AgentStep.tool_calls))
            .where(AgentRun.id == agent_run_id)
        ).scalar_one_or_none()

    def list(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentRun], int]:
        conditions = []
        if kind:
            conditions.append(AgentRun.agent_kind == kind)
        if status:
            conditions.append(AgentRun.status == status)
        total = (
            self.db.scalar(select(func.count()).select_from(AgentRun).where(*conditions)) or 0
        )
        items = (
            self.db.execute(
                select(AgentRun)
                .where(*conditions)
                .order_by(AgentRun.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(items), total

    def create(
        self,
        *,
        agent_kind: str,
        trigger: dict,
        model: str | None,
        budget_max_steps: int | None,
        budget_max_tokens: int | None,
        budget_max_usd: float | None,
    ) -> AgentRun:
        run = AgentRun(
            agent_kind=agent_kind,
            trigger=trigger,
            model=model,
            status="running",
            budget_max_steps=budget_max_steps,
            budget_max_tokens=budget_max_tokens,
            budget_max_usd=budget_max_usd,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def add_step(
        self,
        run: AgentRun,
        *,
        step_no: int,
        request_summary: dict | None,
        response_summary: dict | None,
        stop_reason: str | None,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cost_usd: float,
    ) -> AgentStep:
        step = AgentStep(
            agent_run_id=run.id,
            step_no=step_no,
            request_summary=request_summary,
            response_summary=response_summary,
            stop_reason=stop_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
        self.db.add(step)
        self.db.flush()
        return step

    def apply_step_usage(
        self, run: AgentRun, *, tokens_in: int, tokens_out: int, cost_usd: float
    ) -> None:
        """Roll one step's usage up onto the run row -- the budget columns
        services/agents/budgets.py checks between turns."""
        run.steps_used += 1
        run.tokens_in += tokens_in
        run.tokens_out += tokens_out
        run.cost_usd = float(run.cost_usd) + cost_usd
        self.db.flush()

    def add_tool_call(
        self, step: AgentStep, *, tool_name: str, args: dict
    ) -> AgentToolCall:
        call = AgentToolCall(agent_step_id=step.id, tool_name=tool_name, args=args)
        self.db.add(call)
        self.db.flush()
        return call

    def finish_tool_call(
        self,
        call: AgentToolCall,
        *,
        result_summary: dict | None,
        is_error: bool,
        latency_ms: int,
    ) -> None:
        call.result_summary = result_summary
        call.is_error = is_error
        call.latency_ms = latency_ms
        self.db.flush()

    def set_status(
        self, run: AgentRun, status: str, *, outcome: dict | None = None
    ) -> None:
        run.status = status
        if outcome is not None:
            run.outcome = outcome
        self.db.flush()
