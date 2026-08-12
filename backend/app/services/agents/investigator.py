"""Exception investigator (docs/plan.md §3, row 2): one quarantined
document at a time, through a forensic tool surface (bytes head, re-sniff,
parser attempts, OCR at chosen DPI, page images), ending in exactly one of
the contract's outcomes -- resolved (resolve_quarantine re-enqueues with a
corrected route) or an escalation with a structured diagnosis.

Trigger wiring: the queue is `quarantines.status = 'open'` --
investigate_open_quarantines() batches over it, one budgeted run per
document (12 steps / 60K tokens / $0.50, config-defaulted).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AgentRun, Document, Quarantine
from app.services.agents.budgets import Budget
from app.services.agents.model_client import ModelClient
from app.services.agents.runner import AgentDefinition, AgentRunner
from app.services.agents.tools import ToolContext

logger = get_logger("agents.investigator")

SYSTEM_PROMPT = """\
You are the exception investigator for a breach-analytics pipeline. One
document failed deterministic processing and was quarantined; your job is to
diagnose WHY from evidence, then either fix its routing or hand it to a human
with a precise diagnosis.

Method: start from get_document_meta; look at the actual bytes
(get_raw_bytes_head, sniff_type) before theorizing; test hypotheses cheaply
(try_parser with the class the bytes suggest; run_ocr at a higher DPI or with
preprocessing for scan-quality failures; get_page_image only when you must see
the page). Never guess from the filename -- extensions lie, bytes do not.

End with exactly one of:
- resolve_quarantine: when a corrected route or parameter demonstrably works
  (you saw try_parser or run_ocr succeed), with the fix in `resolution`.
- escalate: when the file is genuinely unprocessable (encrypted, truncated,
  junk), with a diagnosis stating what you tested and what the evidence shows.
Never resolve on an untested hypothesis.\
"""

TOOL_NAMES = (
    "get_document_meta",
    "get_raw_bytes_head",
    "sniff_type",
    "try_parser",
    "run_ocr",
    "get_page_image",
    "resolve_quarantine",
    "escalate",
)


def _build_prompt(db: Session, trigger: dict) -> str:
    return (
        f"Investigate quarantined document {trigger.get('document_id')} "
        f"(quarantine {trigger.get('quarantine_id')}). Recorded reason: "
        f"{trigger.get('reason_code')} -- {trigger.get('detail') or 'no detail'}. "
        "Diagnose from evidence, then resolve_quarantine or escalate."
    )


def _finalize(db: Session, ctx: ToolContext, final_text: str) -> tuple[str, dict]:
    if ctx.escalation is not None:
        return "escalated", {"escalation": ctx.escalation, "summary": final_text}
    if ctx.resolution is not None:
        return "succeeded", {"resolved": True, "resolution": ctx.resolution, "summary": final_text}
    # The model concluded without either terminal tool -- the run itself
    # succeeded, but the quarantine is still open and the outcome says so.
    return "succeeded", {"resolved": False, "summary": final_text}


DEFINITION = AgentDefinition(
    kind="investigator",
    system_prompt=SYSTEM_PROMPT,
    tool_names=TOOL_NAMES,
    build_prompt=_build_prompt,
    finalize=_finalize,
)


def build_trigger(db: Session, quarantine: Quarantine) -> dict:
    document = db.get(Document, quarantine.document_id)
    return {
        "quarantine_id": str(quarantine.id),
        "document_id": str(quarantine.document_id),
        "run_id": str(document.run_id) if document else None,
        "reason_code": quarantine.reason_code,
        "detail": quarantine.detail,
    }


def investigate_quarantine(
    db: Session,
    model_client: ModelClient,
    quarantine_id: uuid.UUID,
    *,
    budget: Budget | None = None,
) -> AgentRun:
    quarantine = db.get(Quarantine, quarantine_id)
    if quarantine is None:
        raise ValueError(f"quarantine {quarantine_id} not found")
    trigger = build_trigger(db, quarantine)
    return AgentRunner(model_client).run(db, DEFINITION, trigger, budget=budget)


def investigate_open_quarantines(
    db: Session, model_client: ModelClient, *, limit: int = 10
) -> list[AgentRun]:
    """The batch trigger (docs/plan.md §3: quarantine rows, queued): one
    budgeted run per open quarantine, oldest first. Per-document isolation
    is the runner's own failure semantics -- one failed run never stops the
    batch."""
    quarantines = (
        db.execute(
            select(Quarantine)
            .where(Quarantine.status == "open")
            .order_by(Quarantine.created_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    runs: list[AgentRun] = []
    for quarantine in quarantines:
        runs.append(investigate_quarantine(db, model_client, quarantine.id))
        logger.info(
            "investigator_dispatched",
            quarantine_id=str(quarantine.id),
            agent_run_id=str(runs[-1].id),
            status=runs[-1].status,
        )
    return runs
