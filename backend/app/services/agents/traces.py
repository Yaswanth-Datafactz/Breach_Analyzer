"""Trace persistence with the commit-BEFORE-execute contract (docs/plan.md
§3's AgentRunner mechanics: "every step + tool call persisted BEFORE
execution continues (a crash still leaves an inspectable trace)").

The recorder owns the commits:
- record_step: the model turn's step row + run rollups + cost_events row,
  committed before any of the turn's tools run.
- begin_tool_call: the tool row with its args, committed before the handler
  executes -- a kill -9 mid-handler leaves the row proving what was asked.
- finish_tool_call: result summary / is_error / latency, committed after.

Structured logs never carry tool payloads (they can contain evidence text);
they carry names, ids, and sizes -- the PII-redacting processor is the
backstop, this is the policy.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AgentRun, AgentStep, AgentToolCall, CostEvent
from app.repositories.agent_runs import AgentRunRepository
from app.services.agents.model_client import ModelTurn

logger = get_logger("agents.traces")

_TEXT_SUMMARY_CHARS = 800


class TraceRecorder:
    def __init__(self, db: Session, run: AgentRun):
        self.db = db
        self.run = run
        self._repo = AgentRunRepository(db)

    def record_step(
        self,
        *,
        turn: ModelTurn,
        latency_ms: int,
        cost_usd: float,
        tool_names: list[str],
        message_count: int,
        run_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
    ) -> AgentStep:
        """Persist the model turn as an agent_steps row + the run rollups +
        the cost_events row, in ONE commit that lands before any tool
        executes."""
        step = self._repo.add_step(
            self.run,
            step_no=self.run.steps_used + 1,
            request_summary={
                "message_count": message_count,
                "tools_offered": tool_names,
            },
            response_summary={
                "text": turn.text[:_TEXT_SUMMARY_CHARS],
                "tool_calls": [
                    {"id": call.id, "name": call.name} for call in turn.tool_calls
                ],
            },
            stop_reason=turn.stop_reason,
            tokens_in=turn.input_tokens,
            tokens_out=turn.output_tokens,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
        self._repo.apply_step_usage(
            self.run,
            tokens_in=turn.input_tokens,
            tokens_out=turn.output_tokens,
            cost_usd=cost_usd,
        )
        self.db.add(
            CostEvent(
                run_id=run_id,
                purpose=f"agent_{self.run.agent_kind}",
                model=self.run.model or "unknown",
                document_id=document_id,
                agent_run_id=self.run.id,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                cached_input_tokens=turn.cached_input_tokens,
                cost_usd=cost_usd,
            )
        )
        self.db.commit()
        logger.info(
            "agent_step",
            agent_run_id=str(self.run.id),
            step_no=step.step_no,
            stop_reason=turn.stop_reason,
            tool_calls=[call.name for call in turn.tool_calls],
            cost_usd=cost_usd,
        )
        return step

    def begin_tool_call(self, step: AgentStep, *, tool_name: str, args: dict) -> AgentToolCall:
        """Committed BEFORE the handler runs -- the trace-before-execute
        half that makes a crash mid-tool inspectable."""
        call = self._repo.add_tool_call(step, tool_name=tool_name, args=args)
        self.db.commit()
        return call

    def finish_tool_call(
        self,
        call: AgentToolCall,
        *,
        result_summary: dict | None,
        is_error: bool,
        latency_ms: int,
    ) -> None:
        self._repo.finish_tool_call(
            call, result_summary=result_summary, is_error=is_error, latency_ms=latency_ms
        )
        self.db.commit()
        logger.info(
            "agent_tool_call",
            agent_run_id=str(self.run.id),
            tool=call.tool_name,
            is_error=is_error,
            latency_ms=latency_ms,
        )

    def finish_run(self, status: str, outcome: dict | None) -> None:
        self._repo.set_status(self.run, status, outcome=outcome)
        self.db.commit()
        logger.info(
            "agent_run_finished",
            agent_run_id=str(self.run.id),
            agent_kind=self.run.agent_kind,
            status=status,
            steps_used=self.run.steps_used,
            cost_usd=float(self.run.cost_usd),
        )
