"""/agents (docs/plan.md §5): observe agent runs (list, full trace with
budget usage), dispatch runs manually, and work the human approval gates.

Dispatch is LIVE-ONLY: POST /agents/runs requires OPENAI_API_KEY (docs/
plan.md D3 swap, 2026-08-12: Anthropic replaced with OpenAI) and answers
409 with the standard error envelope when it is absent -- never a crash,
never a silent fake run (docs/plan.md: the keyed run is config-only).
Approval decisions resume a parked run inline when its scripted client is
still in this process; live-client runs are marked resumable instead of
blocking the HTTP request on a minutes-long model loop.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.security import require_api_key
from app.db.models import AgentRun
from app.db.session import SessionLocal, get_db
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.approvals import ApprovalRepository
from app.schemas.agent import (
    AgentKind,
    AgentRunCreateIn,
    AgentRunDetailOut,
    AgentRunOut,
    AgentRunPageOut,
    AgentStepOut,
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    ApprovalOut,
    ApprovalPageOut,
    BudgetOut,
)
from app.schemas.errors import ErrorEnvelope
from app.services.agents import adjudicator, auditor, investigator, orchestrator
from app.services.agents.budgets import Budget, budget_for_kind, with_overrides
from app.services.agents.model_client import real_model_client_or_none
from app.services.agents.runner import (
    AgentDefinition,
    AgentRunner,
    is_parked,
    parked_client_is_scripted,
    resume_parked,
)

logger = get_logger("api.agents")

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)

DEFINITIONS: dict[str, AgentDefinition] = {
    "orchestrator": orchestrator.DEFINITION,
    "investigator": investigator.DEFINITION,
    "adjudicator": adjudicator.DEFINITION,
    "auditor": auditor.DEFINITION,
}


def _detail(run: AgentRun) -> AgentRunDetailOut:
    base = AgentRunOut.model_validate(run)
    return AgentRunDetailOut(
        **base.model_dump(),
        steps=[AgentStepOut.model_validate(step) for step in run.steps],
        budget=BudgetOut(
            max_steps=run.budget_max_steps,
            max_tokens=run.budget_max_tokens,
            max_usd=float(run.budget_max_usd) if run.budget_max_usd is not None else None,
            steps_used=run.steps_used,
            tokens_used=run.tokens_in + run.tokens_out,
            cost_usd=float(run.cost_usd),
        ),
    )


@router.get("/runs", response_model=AgentRunPageOut)
def list_agent_runs(
    kind: AgentKind | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> AgentRunPageOut:
    items, total = AgentRunRepository(db).list(
        kind=kind, status=status_filter, limit=limit, offset=offset
    )
    return AgentRunPageOut(
        items=[AgentRunOut.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/runs/{agent_run_id}",
    response_model=AgentRunDetailOut,
    responses={404: {"description": "Agent run not found", "model": ErrorEnvelope}},
)
def get_agent_run(agent_run_id: uuid.UUID, db: Session = Depends(get_db)) -> AgentRunDetailOut:
    run = AgentRunRepository(db).get_with_trace(agent_run_id)
    if run is None:
        raise NotFoundError(f"Agent run {agent_run_id} not found")
    return _detail(run)


def _execute_dispatched_run(agent_run_id: uuid.UUID, kind: str, budget: Budget) -> None:
    """Background task body: its own session, never raises (a failed run is
    recorded on the row by the runner; a crash before the loop is logged)."""
    db = SessionLocal()
    try:
        client = real_model_client_or_none()
        run = AgentRunRepository(db).get(agent_run_id)
        if client is None or run is None:  # pragma: no cover - guarded at dispatch
            logger.error("agent_dispatch_lost", agent_run_id=str(agent_run_id))
            return
        AgentRunner(client).run(db, DEFINITIONS[kind], dict(run.trigger), budget=budget, agent_run=run)
    except Exception:
        db.rollback()
        logger.exception("agent_dispatch_crashed", agent_run_id=str(agent_run_id))
    finally:
        db.close()


@router.post(
    "/runs",
    response_model=AgentRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        409: {
            "description": "Live dispatch requires OPENAI_API_KEY",
            "model": ErrorEnvelope,
        }
    },
)
def dispatch_agent_run(
    body: AgentRunCreateIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> AgentRunOut:
    settings = get_settings()
    if not settings.openai_api_key:
        raise ConflictError(
            "Live agent dispatch requires OPENAI_API_KEY. Set it in backend/.env "
            "and restart -- no code change is needed; without a key the agent layer "
            "runs only against scripted clients (tests/demo traces).",
            details={"kind": body.kind, "missing": "OPENAI_API_KEY"},
        )
    unit_count = len(body.trigger.get("flag_ids", [])) if body.kind == "auditor" else 1
    budget = with_overrides(
        budget_for_kind(body.kind, settings, unit_count=max(1, unit_count)),
        max_steps=body.budget_max_steps,
        max_tokens=body.budget_max_tokens,
        max_usd=body.budget_max_usd,
        force_failure=body.force_failure,
    )
    run = AgentRunRepository(db).create(
        agent_kind=body.kind,
        trigger=body.trigger,
        model=settings.agent_model,
        budget_max_steps=budget.max_steps,
        budget_max_tokens=budget.max_tokens,
        budget_max_usd=budget.max_usd,
    )
    db.commit()  # the background task reads the row from its own session
    background_tasks.add_task(_execute_dispatched_run, run.id, body.kind, budget)
    logger.info(
        "agent_run_dispatched",
        agent_run_id=str(run.id),
        kind=body.kind,
        force_failure=body.force_failure,
    )
    return AgentRunOut.model_validate(run)


@router.get("/approvals", response_model=ApprovalPageOut)
def list_approvals(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> ApprovalPageOut:
    items, total = ApprovalRepository(db).list(
        status=status_filter, limit=limit, offset=offset
    )
    return ApprovalPageOut(
        items=[ApprovalOut.model_validate(a) for a in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=ApprovalDecisionOut,
    responses={
        404: {"description": "Approval not found", "model": ErrorEnvelope},
        409: {"description": "Approval already decided", "model": ErrorEnvelope},
    },
)
def decide_approval(
    approval_id: uuid.UUID,
    body: ApprovalDecisionIn,
    db: Session = Depends(get_db),
) -> ApprovalDecisionOut:
    approvals = ApprovalRepository(db)
    approval = approvals.get(approval_id)
    if approval is None:
        raise NotFoundError(f"Approval {approval_id} not found")
    if approval.status != "pending":
        raise ConflictError(
            f"Approval {approval_id} was already {approval.status}",
            details={"decided_by": approval.decided_by},
        )
    approvals.decide(approval, status=body.decision, decided_by=body.decided_by)
    db.commit()

    runs = AgentRunRepository(db)
    run = runs.get(approval.agent_run_id)
    resumed = False
    if is_parked(run.id):
        if parked_client_is_scripted(run.id):
            # Scripted client: continuing costs nothing -- resume inline so
            # the caller sees the completed run in this same response.
            run = resume_parked(db, run.id, decided_by=body.decided_by) or run
            resumed = True
        else:
            # Live client: a resume is a minutes-long model loop -- record
            # the decision and mark the run resumable instead of blocking.
            outcome = dict(run.outcome or {})
            outcome["approval_decided"] = body.decision
            outcome["resumable"] = True
            runs.set_status(run, "awaiting_approval", outcome=outcome)
            db.commit()
    else:
        outcome = dict(run.outcome or {})
        outcome["approval_decided"] = body.decision
        outcome["resume"] = "unavailable (parked state not held by this process)"
        runs.set_status(run, run.status, outcome=outcome)
        db.commit()
    logger.info(
        "approval_decided",
        approval_id=str(approval_id),
        decision=body.decision,
        agent_run_id=str(run.id),
        resumed=resumed,
    )
    return ApprovalDecisionOut(
        approval=ApprovalOut.model_validate(approval),
        run_status=run.status,
        resumed=resumed,
    )
