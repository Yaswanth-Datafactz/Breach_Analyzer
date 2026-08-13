"""Orchestrator agent (docs/plan.md §3, row 1): judgment calls at run
checkpoints -- batch ordering by identity-yield-per-dollar, anomaly
reactions -- emitted as typed CampaignDirectives from a closed menu. It
adapts within the menu; it never invents pipeline behavior and never touches
documents.

The directives applier is the deterministic half: it validates every
directive against the menu + the matching tool's own Pydantic args model,
refuses anything out-of-menu, and folds the accepted ones into a campaign
state (batch priorities / escalation threshold) that the pipeline reads via
latest_campaign_state(). Application only ever mutates run priorities and
thresholds -- nothing else.

Trigger wiring: the pipeline's checkpoint hook calls dispatch_checkpoint(),
which is a logged NO-OP without OPENAI_API_KEY -- a keyless environment
runs the whole pipeline with zero model calls and zero crashes.
"""

from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AgentRun
from app.schemas.agent import CampaignDirective
from app.services.agents.runner import AgentDefinition, AgentRunner
from app.services.agents.tools import REGISTRY, ToolContext, jsonable

logger = get_logger("agents.orchestrator")

SYSTEM_PROMPT = """\
You are the processing-run orchestrator for a breach-analytics pipeline. At run
checkpoints you review live statistics and decide, from a fixed menu, how the
deterministic pipeline should adapt: reorder file-class batches
(set_batch_priority), move the tier-2 escalation threshold
(set_escalation_threshold), queue the exception investigator on specific
quarantines (dispatch_investigator), or request human sign-off
(request_approval). You never touch documents directly.

Method: call get_run_stats and get_class_failure_rates first; list_quarantines
when failures cluster. Issue directives only when the numbers justify them --
a healthy run needs none. Every directive's reason must cite the statistic
that motivated it. Finish with a short prose summary of the run's health and
the directives you issued.\
"""

TOOL_NAMES = (
    "get_run_stats",
    "get_class_failure_rates",
    "list_quarantines",
    "set_batch_priority",
    "set_escalation_threshold",
    "dispatch_investigator",
    "request_approval",
)

# The closed action menu (docs/plan.md §3). Params for each action validate
# against the SAME args model as the tool of that name -- one schema, two
# enforcement points.
DIRECTIVE_MENU = frozenset(
    {"set_batch_priority", "set_escalation_threshold", "dispatch_investigator", "request_approval"}
)


def _build_prompt(db: Session, trigger: dict) -> str:
    checkpoint = trigger.get("checkpoint", "manual")
    run_id = trigger.get("run_id", "unknown")
    return (
        f"Checkpoint '{checkpoint}' for processing run {run_id}. Review the run's "
        "statistics and issue campaign directives if, and only if, the numbers "
        "call for them. Then summarize."
    )


def apply_directives(db: Session, directives: list[dict]) -> dict:
    """Validate + apply the recorded directives. Out-of-menu actions and
    schema-invalid params are REFUSED (recorded, never applied). Returns
    {'applied': [...], 'refused': [...], 'campaign': {...}} -- `campaign` is
    the only mutable surface: batch priorities and the escalation
    threshold."""
    applied: list[dict] = []
    refused: list[dict] = []
    campaign: dict = {"batch_priorities": {}, "escalation_threshold": None}
    for raw in directives:
        try:
            directive = CampaignDirective.model_validate(raw)
        except ValidationError as exc:
            refused.append({"directive": raw, "reason": f"not in menu: {exc.errors()[0]['msg']}"})
            continue
        if directive.action not in DIRECTIVE_MENU:  # belt over the Literal's braces
            refused.append({"directive": raw, "reason": "action not in menu"})
            continue
        spec = REGISTRY[directive.action]
        try:
            params = spec.args_model.model_validate(directive.params)
        except ValidationError as exc:
            refused.append(
                {"directive": raw, "reason": f"invalid params: {exc.errors()[0]['msg']}"}
            )
            continue
        if directive.action == "set_batch_priority":
            campaign["batch_priorities"][params.file_class] = params.priority
        elif directive.action == "set_escalation_threshold":
            campaign["escalation_threshold"] = params.threshold
        # dispatch_investigator / request_approval carry no campaign state --
        # the investigator queue and the approval row are their application.
        applied.append(directive.model_dump(mode="json"))
    return {"applied": applied, "refused": refused, "campaign": jsonable(campaign)}


def _finalize(db: Session, ctx: ToolContext, final_text: str) -> tuple[str, dict]:
    application = apply_directives(db, ctx.directives)
    return (
        "succeeded",
        {
            "directives": application["applied"],
            "refused": application["refused"],
            "campaign": application["campaign"],
            "summary": final_text,
        },
    )


DEFINITION = AgentDefinition(
    kind="orchestrator",
    system_prompt=SYSTEM_PROMPT,
    tool_names=TOOL_NAMES,
    build_prompt=_build_prompt,
    finalize=_finalize,
)


def latest_campaign_state(db: Session, run_id: uuid.UUID) -> dict:
    """What the pipeline should currently honor for this run: campaign
    states from every succeeded orchestrator run over the run, oldest to
    newest, later directives overriding earlier ones."""
    runs = (
        db.execute(
            select(AgentRun)
            .where(AgentRun.agent_kind == "orchestrator", AgentRun.status == "succeeded")
            .order_by(AgentRun.created_at)
        )
        .scalars()
        .all()
    )
    state: dict = {"batch_priorities": {}, "escalation_threshold": None}
    for run in runs:
        if str(run.trigger.get("run_id")) != str(run_id):
            continue
        campaign = (run.outcome or {}).get("campaign") or {}
        state["batch_priorities"].update(campaign.get("batch_priorities") or {})
        if campaign.get("escalation_threshold") is not None:
            state["escalation_threshold"] = campaign["escalation_threshold"]
    return state


def dispatch_checkpoint(db: Session, run_id: uuid.UUID, checkpoint: str) -> AgentRun | None:
    """The pipeline's checkpoint hook target. Without OPENAI_API_KEY this
    is a logged no-op (docs/plan.md: keyless runs are config-only away from
    live) -- the pipeline never learns the difference."""
    from app.services.agents.model_client import real_model_client_or_none

    client = real_model_client_or_none()
    if client is None:
        logger.info(
            "orchestrator_checkpoint_skipped",
            run_id=str(run_id),
            checkpoint=checkpoint,
            reason="no OPENAI_API_KEY",
        )
        return None
    trigger = {"run_id": str(run_id), "checkpoint": checkpoint}
    return AgentRunner(client).run(db, DEFINITION, trigger)
