"""QA auditor (docs/plan.md §3, row 4): a post-run stratified sample of
exposure flags (category x confidence band, ~50 flags), each verdict
structurally grounded -- verify_flag refuses any verdict whose quote is not
re-found verbatim in the flag's evidence passages. Contradictions land in
the review queue (the tool writes review_items); the measured error
estimate lands in the run outcome JSONB via report_estimate.

One run per audit: budget 3 steps/flag x sample size, $2/run
(config-defaulted).
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AgentRun, ExposureFlag, Person
from app.services.agents.budgets import Budget, budget_for_kind
from app.services.agents.model_client import ModelClient
from app.services.agents.runner import AgentDefinition, AgentRunner
from app.services.agents.tools import ToolContext

logger = get_logger("agents.auditor")

SYSTEM_PROMPT = """\
You are the QA auditor for a breach-analytics exposure table. You receive a
stratified sample of exposure flags; for each one, decide from the evidence
whether the flag is justified.

Method, per flag: get_flag_with_evidence, then read the anchoring passage
with get_passage_text. Record your verdict with verify_flag, quoting the
exact span from the passage that grounds it -- the tool rejects any quote it
cannot re-find verbatim, so copy precisely, never paraphrase. `verified`
means the passage genuinely exposes that category for that person;
`contradicted` means it does not (trap value, wrong person, wrong category).

After the last flag, call report_estimate with your counts and the
extrapolated error rate, then summarize the failure patterns you saw.\
"""

TOOL_NAMES = (
    "get_flag_with_evidence",
    "get_passage_text",
    "verify_flag",
    "report_estimate",
)

_BANDS = ((0.5, "low"), (0.8, "mid"), (1.01, "high"))


def _band(confidence: float | None) -> str:
    if confidence is None:
        return "unscored"
    for ceiling, name in _BANDS:
        if float(confidence) < ceiling:
            return name
    return "high"


def stratified_flag_sample(
    db: Session, *, run_id: uuid.UUID | None = None, target: int = 50
) -> list[ExposureFlag]:
    """~`target` exposed flags spread across category x confidence band
    (docs/plan.md §3's auditor trigger): round-robin over the cells so no
    band or category dominates the audit."""
    query = select(ExposureFlag).where(ExposureFlag.exposed.is_(True))
    if run_id is not None:
        query = query.join(Person, ExposureFlag.person_id == Person.id).where(
            Person.run_id == run_id
        )
    flags = db.execute(query.order_by(ExposureFlag.created_at)).scalars().all()
    cells: dict[tuple[str, str], list[ExposureFlag]] = defaultdict(list)
    for flag in flags:
        cells[(flag.category, _band(flag.confidence))].append(flag)
    sample: list[ExposureFlag] = []
    ordered_cells = [cells[key] for key in sorted(cells)]
    index = 0
    while len(sample) < target and any(ordered_cells):
        for cell in ordered_cells:
            if index < len(cell) and len(sample) < target:
                sample.append(cell[index])
        index += 1
        if all(index >= len(cell) for cell in ordered_cells):
            break
    return sample


def _build_prompt(db: Session, trigger: dict) -> str:
    flag_ids = trigger.get("flag_ids", [])
    listing = "\n".join(f"- {flag_id}" for flag_id in flag_ids)
    return (
        f"Audit these {len(flag_ids)} exposure flags (stratified sample):\n"
        f"{listing}\n"
        "For each: get_flag_with_evidence, get_passage_text, verify_flag with a "
        "verbatim quote. Then report_estimate and summarize."
    )


def _finalize(db: Session, ctx: ToolContext, final_text: str) -> tuple[str, dict]:
    return (
        "succeeded",
        {
            "verdicts": ctx.verdicts,
            "contradicted": sum(1 for v in ctx.verdicts if v["verdict"] == "contradicted"),
            "estimate": ctx.estimate,  # the measured error estimate JSONB
            "summary": final_text,
        },
    )


DEFINITION = AgentDefinition(
    kind="auditor",
    system_prompt=SYSTEM_PROMPT,
    tool_names=TOOL_NAMES,
    build_prompt=_build_prompt,
    finalize=_finalize,
)


def audit_flags(
    db: Session,
    model_client: ModelClient,
    flag_ids: list[uuid.UUID],
    *,
    budget: Budget | None = None,
) -> AgentRun:
    from app.core.config import get_settings

    trigger = {"flag_ids": [str(f) for f in flag_ids]}
    budget = budget or budget_for_kind("auditor", get_settings(), unit_count=len(flag_ids))
    run = AgentRunner(model_client).run(db, DEFINITION, trigger, budget=budget)
    logger.info(
        "auditor_finished",
        agent_run_id=str(run.id),
        flags=len(flag_ids),
        status=run.status,
    )
    return run


def audit_run(
    db: Session,
    model_client: ModelClient,
    *,
    run_id: uuid.UUID | None = None,
    target: int = 50,
) -> AgentRun | None:
    """The post-run trigger: sample, then audit. None when there is nothing
    to audit yet (no exposed flags)."""
    sample = stratified_flag_sample(db, run_id=run_id, target=target)
    if not sample:
        logger.info("auditor_skipped", reason="no exposed flags to sample")
        return None
    return audit_flags(db, model_client, [flag.id for flag in sample])
