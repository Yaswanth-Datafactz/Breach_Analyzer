"""Pydantic models for /api/v1/agents (Handbook §6.2: Pydantic models on
every endpoint) plus the orchestrator's typed CampaignDirective contract
(docs/plan.md §3: directives are validated & applied by the runner from a
typed menu -- the model never invents pipeline behavior)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

AgentKind = Literal["orchestrator", "investigator", "adjudicator", "auditor"]

# The orchestrator's action menu (docs/plan.md §3's orchestrator row):
# anything outside this closed set is refused by the directives applier.
DirectiveAction = Literal[
    "set_batch_priority",
    "set_escalation_threshold",
    "dispatch_investigator",
    "request_approval",
]


class CampaignDirective(BaseModel):
    """One orchestrator decision from the typed menu. `params` is validated
    against the matching tool's own args model by the applier -- a directive
    is exactly as constrained as the tool it names."""

    action: DirectiveAction
    params: dict = Field(default_factory=dict)


class AgentRunCreateIn(BaseModel):
    """POST /agents/runs: manual dispatch of a LIVE agent run (requires
    ANTHROPIC_API_KEY -- 409 otherwise). Budget overrides replace the
    config defaults for this run only; `force_failure` is the scripted demo
    knob (docs/plan.md §3: investigator vs the password-protected PDF at a
    4-step budget -> budget_exceeded with diagnosis-so-far in the trace)."""

    kind: AgentKind
    trigger: dict = Field(default_factory=dict)
    budget_max_steps: int | None = Field(default=None, ge=1)
    budget_max_tokens: int | None = Field(default=None, ge=1)
    budget_max_usd: float | None = Field(default=None, gt=0)
    force_failure: bool = False


class AgentToolCallOut(BaseModel):
    id: UUID
    tool_name: str
    args: dict
    result_summary: dict | None
    is_error: bool
    latency_ms: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentStepOut(BaseModel):
    id: UUID
    step_no: int
    request_summary: dict | None
    response_summary: dict | None
    stop_reason: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int | None
    cost_usd: float | None
    created_at: datetime
    tool_calls: list[AgentToolCallOut]

    model_config = {"from_attributes": True}


class BudgetOut(BaseModel):
    """Max vs used, side by side -- the UI's budget meter renders from this."""

    max_steps: int | None
    max_tokens: int | None
    max_usd: float | None
    steps_used: int
    tokens_used: int
    cost_usd: float


class AgentRunOut(BaseModel):
    id: UUID
    agent_kind: str
    trigger: dict
    model: str | None
    status: str
    budget_max_steps: int | None
    budget_max_tokens: int | None
    budget_max_usd: float | None
    steps_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    outcome: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentRunDetailOut(AgentRunOut):
    steps: list[AgentStepOut]
    budget: BudgetOut


class AgentRunPageOut(BaseModel):
    items: list[AgentRunOut]
    total: int
    limit: int
    offset: int


class ApprovalOut(BaseModel):
    id: UUID
    agent_run_id: UUID
    action_type: str
    payload: dict
    status: str
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalPageOut(BaseModel):
    items: list[ApprovalOut]
    total: int
    limit: int
    offset: int


class ApprovalDecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1, max_length=200)
    note: str | None = None


class ApprovalDecisionOut(BaseModel):
    """The decision result: the updated approval, what happened to the
    parked run (`resumed` -- inline for scripted/fake clients; real-client
    runs are marked resumable instead of blocking the request on a
    minutes-long model loop), and the run's status after the decision."""

    approval: ApprovalOut
    run_status: str
    resumed: bool
