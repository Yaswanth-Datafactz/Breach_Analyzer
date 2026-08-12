"""AgentRunner (docs/plan.md D2/§3): the first-party ~200-line loop over
Anthropic native tool use. `while stop_reason == "tool_use"`, with the three
mechanics the design review grades:

1. Trace-before-execute -- the agent_steps row is committed before the
   turn's tools run, and every agent_tool_calls row is committed (with its
   args) before its handler executes. A crash mid-tool leaves the trace.
2. Budgets between turns -- steps/tokens/USD read off the run row's own
   rollups; any exhausted dimension parks the run `budget_exceeded` with
   partial findings, never mid-step.
3. Approval gates -- a gate tool returning pending_approval parks the run
   `awaiting_approval` (state held in _PARKED for this process); resume()
   applies the human decision deterministically, injects it as the tool
   result, and continues the same loop.

The runner takes a ModelClient (model_client.py) -- the scripted fake in
tests, the real Anthropic adapter only when a key exists. It never
constructs a client itself.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AgentRun, AgentStep, Document, ProcessingRun
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.approvals import ApprovalRepository
from app.services.agents.budgets import Budget, budget_for_kind, step_cost_usd
from app.services.agents.model_client import ModelClient, ModelToolCall, tool_result_block
from app.services.agents.tools import (
    ToolContext,
    ToolResult,
    anthropic_tool_definitions,
    apply_approved_action,
    jsonable,
    run_tool,
)
from app.services.agents.traces import TraceRecorder

logger = get_logger("agents.runner")


@dataclass(frozen=True)
class AgentDefinition:
    """One agent = a system prompt + a tool subset + an output contract
    (docs/plan.md §3's table). The runner stays generic; everything
    kind-specific lives on this object."""

    kind: str
    system_prompt: str
    tool_names: tuple[str, ...]
    build_prompt: Callable[[Session, dict], str]
    # (db, ctx, final_text) -> (terminal_status, outcome) per the §4 enum.
    finalize: Callable[[Session, ToolContext, str], tuple[str, dict]]


@dataclass
class _ParkedRun:
    """Everything needed to continue a gated loop after the human answers.
    In-memory by construction: a restart loses only the ability to resume
    inline -- the run row, trace, and approval survive in the DB."""

    definition: AgentDefinition
    model_client: ModelClient
    ctx: ToolContext
    budget: Budget
    messages: list[dict]
    step_id: uuid.UUID
    pending_call: ModelToolCall
    completed_blocks: list[dict]
    remaining_calls: tuple[ModelToolCall, ...]
    approval_id: uuid.UUID


_PARKED: dict[uuid.UUID, _ParkedRun] = {}


def is_parked(agent_run_id: uuid.UUID) -> bool:
    return agent_run_id in _PARKED


def parked_client_is_scripted(agent_run_id: uuid.UUID) -> bool:
    parked = _PARKED.get(agent_run_id)
    return bool(parked and getattr(parked.model_client, "is_scripted", False))


def resume_parked(db: Session, agent_run_id: uuid.UUID, *, decided_by: str) -> AgentRun | None:
    """Resume a parked run with ITS OWN model client (the one that was
    mid-conversation when the gate fired). None when this process holds no
    parked state -- the caller then marks the run resumable instead."""
    parked = _PARKED.pop(agent_run_id, None)
    if parked is None:
        return None
    run = AgentRunRepository(db).get(agent_run_id)
    if run is None:  # pragma: no cover - parked state implies the row exists
        return None
    return AgentRunner(parked.model_client, model=run.model)._continue(
        db, run, parked, decided_by
    )


def _scope_ids(db: Session, trigger: dict) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """run_id/document_id for cost_events attribution -- only when the
    trigger's ids resolve to real rows (FKs must never abort a step commit)."""
    run_id = document_id = None
    try:
        if trigger.get("run_id") and db.get(ProcessingRun, uuid.UUID(str(trigger["run_id"]))):
            run_id = uuid.UUID(str(trigger["run_id"]))
        if trigger.get("document_id") and db.get(Document, uuid.UUID(str(trigger["document_id"]))):
            document_id = uuid.UUID(str(trigger["document_id"]))
    except ValueError:
        pass
    return run_id, document_id


def _progress(ctx: ToolContext) -> dict:
    """Partial findings snapshot -- what a parked/exceeded run still shows."""
    return jsonable(
        {
            key: value
            for key, value in (
                ("directives", ctx.directives),
                ("decision", ctx.decision),
                ("resolution", ctx.resolution),
                ("escalation", ctx.escalation),
                ("verdicts", ctx.verdicts),
                ("estimate", ctx.estimate),
            )
            if value
        }
    )


def _result_summary(result: ToolResult) -> dict:
    payload = {k: v for k, v in result.payload.items() if k != "_image_base64"}
    return {"is_error": result.is_error, "payload": payload}


class AgentRunner:
    def __init__(self, model_client: ModelClient, *, model: str | None = None):
        self.model_client = model_client
        self.model = model or get_settings().agent_model

    def run(
        self,
        db: Session,
        definition: AgentDefinition,
        trigger: dict,
        *,
        budget: Budget | None = None,
        agent_run: AgentRun | None = None,
    ) -> AgentRun:
        budget = budget or budget_for_kind(definition.kind, get_settings())
        if agent_run is None:
            agent_run = AgentRunRepository(db).create(
                agent_kind=definition.kind,
                trigger=jsonable(trigger),
                model=self.model,
                budget_max_steps=budget.max_steps,
                budget_max_tokens=budget.max_tokens,
                budget_max_usd=budget.max_usd,
            )
            db.commit()
        ctx = ToolContext(
            agent_run_id=agent_run.id, agent_kind=definition.kind, trigger=trigger
        )
        messages = [{"role": "user", "content": definition.build_prompt(db, trigger)}]
        return self._loop(db, agent_run, definition, ctx, budget, messages)

    def _continue(self, db: Session, run: AgentRun, parked: _ParkedRun, decided_by: str) -> AgentRun:
        """Continue a parked loop: apply the human decision deterministically
        (apply_approved_action), inject it as the gate tool's result, finish
        any tool calls the park interrupted, then re-enter the loop."""
        repo = AgentRunRepository(db)
        approval = ApprovalRepository(db).get(parked.approval_id)
        repo.set_status(run, "running")
        applied = apply_approved_action(db, approval, parked.ctx)
        db.commit()
        logger.info(
            "agent_run_resumed",
            agent_run_id=str(run.id),
            approval_status=approval.status,
            decided_by=decided_by,
        )
        blocks = list(parked.completed_blocks)
        blocks.append(tool_result_block(parked.pending_call.id, jsonable(applied), is_error=False))
        trace = TraceRecorder(db, run)
        step = db.get(AgentStep, parked.step_id)
        state = self._execute_calls(
            db, run, trace, step, parked.remaining_calls, parked.ctx, blocks,
            definition=parked.definition, budget=parked.budget, messages=parked.messages,
        )
        if state is not None:
            return run
        parked.messages.append({"role": "user", "content": blocks})
        return self._loop(db, run, parked.definition, parked.ctx, parked.budget, parked.messages)

    # -- the loop ----------------------------------------------------------

    def _loop(
        self,
        db: Session,
        run: AgentRun,
        definition: AgentDefinition,
        ctx: ToolContext,
        budget: Budget,
        messages: list[dict],
    ) -> AgentRun:
        trace = TraceRecorder(db, run)
        tools = anthropic_tool_definitions(list(definition.tool_names))
        run_scope, doc_scope = _scope_ids(db, ctx.trigger)
        while True:
            dimension = budget.exceeded_dimension(run)
            if dimension is not None:  # checked BETWEEN turns (plan §3)
                trace.finish_run(
                    "budget_exceeded",
                    {"partial": True, "budget_exceeded": dimension, "progress": _progress(ctx)},
                )
                return run
            started = time.monotonic()
            turn = self.model_client.create_turn(
                model=run.model, system=definition.system_prompt, tools=tools, messages=messages
            )
            step = trace.record_step(  # persisted BEFORE any tool executes
                turn=turn,
                latency_ms=int((time.monotonic() - started) * 1000),
                cost_usd=step_cost_usd(
                    run.model, turn.input_tokens, turn.output_tokens, turn.cached_input_tokens
                ),
                tool_names=[t["name"] for t in tools],
                message_count=len(messages),
                run_id=run_scope,
                document_id=doc_scope,
            )
            if turn.stop_reason == "refusal":
                trace.finish_run("failed", {"error": "model_refusal", "progress": _progress(ctx)})
                return run
            messages.append({"role": "assistant", "content": list(turn.raw_content)})
            if not turn.tool_calls:  # stop_reason != tool_use -> the contract parse
                status, outcome = definition.finalize(db, ctx, turn.text)
                trace.finish_run(status, outcome)
                return run
            blocks: list[dict] = []
            state = self._execute_calls(
                db, run, trace, step, turn.tool_calls, ctx, blocks,
                definition=definition, budget=budget, messages=messages,
            )
            if state is not None:  # parked awaiting_approval, or failed -- already recorded
                return run
            if ctx.escalation is not None:  # escalate is terminal (plan §4)
                status, outcome = definition.finalize(db, ctx, turn.text)
                trace.finish_run(status, outcome)
                return run
            messages.append({"role": "user", "content": blocks})

    def _execute_calls(
        self,
        db: Session,
        run: AgentRun,
        trace: TraceRecorder,
        step: AgentStep,
        calls: Sequence[ModelToolCall],
        ctx: ToolContext,
        blocks: list[dict],
        *,
        definition: AgentDefinition,
        budget: Budget,
        messages: list[dict],
    ) -> str | None:
        """Execute one turn's tool calls in order. Returns 'parked'/'failed'
        when the loop must stop (run already finalized), else None."""
        for index, call in enumerate(calls):
            row = trace.begin_tool_call(step, tool_name=call.name, args=jsonable(call.input))
            started = time.monotonic()
            try:
                result = run_tool(db, call.name, call.input, ctx)
            except Exception as exc:  # unexpected handler crash: trace survives
                db.rollback()
                trace.finish_tool_call(
                    row,
                    result_summary={"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                    is_error=True,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                trace.finish_run(
                    "failed",
                    {"error": f"tool '{call.name}' crashed: {type(exc).__name__}",
                     "progress": _progress(ctx)},
                )
                logger.exception("agent_tool_crashed", agent_run_id=str(run.id), tool=call.name)
                return "failed"
            trace.finish_tool_call(
                row,
                result_summary=_result_summary(result),
                is_error=result.is_error,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            if result.pending_approval_id is not None:  # gate -> park (plan §3)
                AgentRunRepository(db).set_status(
                    run,
                    "awaiting_approval",
                    outcome={
                        "awaiting_approval": {
                            "approval_request_id": str(result.pending_approval_id)
                        },
                        "progress": _progress(ctx),
                    },
                )
                db.commit()
                _PARKED[run.id] = _ParkedRun(
                    definition=definition,
                    model_client=self.model_client,
                    ctx=ctx,
                    budget=budget,
                    messages=messages,
                    step_id=step.id,
                    pending_call=call,
                    completed_blocks=list(blocks),
                    remaining_calls=tuple(calls[index + 1 :]),
                    approval_id=result.pending_approval_id,
                )
                logger.info("agent_run_parked", agent_run_id=str(run.id), tool=call.name)
                return "parked"
            blocks.append(tool_result_block(call.id, dict(result.payload), is_error=result.is_error))
        return None
