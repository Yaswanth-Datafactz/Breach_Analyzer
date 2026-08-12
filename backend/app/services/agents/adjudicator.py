"""ER adjudicator (docs/plan.md §3, row 3): one gray-band mention pair per
run, deciding merge | no_merge | escalate with feature citations. The
`decide` tool -- not the prompt -- enforces D5's hard constraints (it
refuses conflicting-strong-identifier and name-only merges outright) and
stops bulk-impact merges at the bulk_merge approval gate. Applied merges go
through the append-only identity_links + er_decisions persist path.

Trigger wiring: get_gray_pairs() reproduces the production ER arithmetic
(normalize -> block -> score -> cluster) over a run's mentions and returns
the gray band; adjudicate_gray_pairs() runs the agent over each pair within
its per-pair budget (10 steps / $0.30, config-defaulted).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AgentRun, Document, Mention, PiiElement
from app.services.agents.budgets import Budget
from app.services.agents.model_client import ModelClient
from app.services.agents.runner import AgentDefinition, AgentRunner
from app.services.agents.tools import ToolContext
from app.services.er.blocking import generate_candidate_pairs
from app.services.er.cluster import cluster_mentions
from app.services.er.normalize import ErMention, build_mention
from app.services.er.scoring import ScoredPair, score_candidates

logger = get_logger("agents.adjudicator")

SYSTEM_PROMPT = """\
You are the entity-resolution adjudicator for a breach-analytics pipeline.
Deterministic scoring left one pair of person-mentions in the gray band --
too similar to dismiss, not corroborated enough to auto-link. Decide whether
they are the same real person.

Method: compare_features first -- the deterministic feature vector is your
primary evidence; read both sides' context with get_mention_evidence; use
search_elements to check whether a shared identifier bridges them through
other documents. Then record exactly one decision with `decide`, citing the
features that carried it.

Rules the tools enforce (do not fight them): conflicting strong identifiers
mean NOT the same person, no matter the name; a name alone -- however similar
-- never merges; wide-impact merges pause for human approval. When evidence
is genuinely balanced, decide escalate rather than guessing.\
"""

TOOL_NAMES = (
    "get_mention_evidence",
    "get_person_summary",
    "compare_features",
    "search_elements",
    "decide",
    "request_approval",
)


def _build_prompt(db: Session, trigger: dict) -> str:
    return (
        f"Adjudicate the gray-band pair: left mention {trigger.get('left_mention_id')} "
        f"vs right mention {trigger.get('right_mention_id')} "
        f"(deterministic score {trigger.get('score', 'unknown')}). "
        "Examine the evidence and record your decision with the decide tool."
    )


def _finalize(db: Session, ctx: ToolContext, final_text: str) -> tuple[str, dict]:
    if ctx.decision is not None:
        status = "escalated" if ctx.decision.get("decision") == "escalate" else "succeeded"
        return status, {"decision": ctx.decision, "summary": final_text}
    return "succeeded", {"decision": None, "summary": final_text}


DEFINITION = AgentDefinition(
    kind="adjudicator",
    system_prompt=SYSTEM_PROMPT,
    tool_names=TOOL_NAMES,
    build_prompt=_build_prompt,
    finalize=_finalize,
)


def _er_mentions_for_run(db: Session, run_id: uuid.UUID) -> list[ErMention]:
    rows = (
        db.execute(
            select(Mention)
            .join(Document, Mention.document_id == Document.id)
            .where(Document.run_id == run_id)
        )
        .scalars()
        .all()
    )
    # Same element-admission rule as the production ER stage (er/persist):
    # the agent's recomputed pairs must be the pairs the pipeline queued,
    # not a trap-and-fake-polluted variant of them.
    from app.services.er.persist import _usable_element

    mentions: list[ErMention] = []
    for row in rows:
        elements = (
            db.execute(select(PiiElement).where(PiiElement.mention_id == row.id))
            .scalars()
            .all()
        )
        mentions.append(
            build_mention(
                str(row.id),
                str(row.document_id),
                name_raw=row.name_raw,
                dob_raw=row.dob.isoformat() if row.dob else None,
                elements=[
                    (e.element_type, e.value_raw)
                    for e in elements
                    if _usable_element(e.validation_status, e.element_type, e.signals)
                ],
            )
        )
    return mentions


def get_gray_pairs(db: Session, run_id: uuid.UUID) -> list[ScoredPair]:
    """The adjudicator's queue (docs/plan.md D5): production blocking +
    scoring + clustering over the run's mentions; the cluster step already
    drops gray pairs whose endpoints auto-linked transitively."""
    settings = get_settings()
    mentions = _er_mentions_for_run(db, run_id)
    if not mentions:
        return []
    blocking = generate_candidate_pairs(mentions)
    scored = score_candidates({m.mention_id: m for m in mentions}, blocking.pairs)
    result = cluster_mentions(
        [m.mention_id for m in mentions],
        scored,
        auto_link_threshold=settings.er_auto_link_threshold,
        distinct_threshold=settings.er_distinct_threshold,
    )
    return result.gray_pairs


def adjudicate_pair(
    db: Session,
    model_client: ModelClient,
    left_mention_id: uuid.UUID,
    right_mention_id: uuid.UUID,
    *,
    score: float | None = None,
    budget: Budget | None = None,
) -> AgentRun:
    left = db.get(Mention, left_mention_id)
    document = db.get(Document, left.document_id) if left else None
    trigger = {
        "left_mention_id": str(left_mention_id),
        "right_mention_id": str(right_mention_id),
        "run_id": str(document.run_id) if document else None,
        "score": score,
    }
    return AgentRunner(model_client).run(db, DEFINITION, trigger, budget=budget)


def adjudicate_gray_pairs(
    db: Session, model_client: ModelClient, run_id: uuid.UUID, *, limit: int = 20
) -> list[AgentRun]:
    runs: list[AgentRun] = []
    for pair in get_gray_pairs(db, run_id)[:limit]:
        runs.append(
            adjudicate_pair(
                db,
                model_client,
                uuid.UUID(pair.left_id),
                uuid.UUID(pair.right_id),
                score=pair.score,
            )
        )
        logger.info(
            "adjudicator_dispatched",
            left=pair.left_id,
            right=pair.right_id,
            agent_run_id=str(runs[-1].id),
            status=runs[-1].status,
        )
    return runs
