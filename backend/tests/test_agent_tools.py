"""Tool-registry tests (docs/plan.md §3's table + §11's validation rule):
completeness against the plan's tool list, Pydantic validation before
execution, and -- graded hardest -- the hard constraints the tools enforce
themselves: decide's conflicting-identifier and name-only refusals, the
bulk_merge gate, and verify_flag's verbatim-quote structural check.

Requires the docker-compose Postgres on :5434 with migrations applied.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import ApprovalRequest, ErDecision, IdentityLink, Passage, ReviewItem
from app.db.session import SessionLocal
from app.repositories.agent_runs import AgentRunRepository
from app.services.agents.tools import (
    REGISTRY,
    ToolContext,
    openai_tool_definitions,
    run_tool,
)
from tests.agent_env import build_agent_env, teardown_agent_env

# The docs/plan.md §3 table, flattened: every tool named there must exist in
# the one registry (D4: single registry, two facades).
PLAN_TABLE_TOOLS = {
    # orchestrator
    "get_run_stats",
    "get_class_failure_rates",
    "list_quarantines",
    "set_batch_priority",
    "set_escalation_threshold",
    "dispatch_investigator",
    "request_approval",
    # investigator
    "get_document_meta",
    "get_raw_bytes_head",
    "sniff_type",
    "try_parser",
    "run_ocr",
    "get_page_image",
    "resolve_quarantine",
    "escalate",
    # adjudicator
    "get_mention_evidence",
    "get_person_summary",
    "compare_features",
    "search_elements",
    "decide",
    # auditor
    "get_flag_with_evidence",
    "get_passage_text",
    "verify_flag",
    "report_estimate",
}


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def env(db):
    environment = build_agent_env(db)
    yield environment
    teardown_agent_env(db, environment)


def test_registry_matches_plan_table():
    assert PLAN_TABLE_TOOLS <= set(REGISTRY), (
        f"missing tools: {PLAN_TABLE_TOOLS - set(REGISTRY)}"
    )
    for spec in REGISTRY.values():
        assert spec.description, spec.name
        assert spec.args_model.model_json_schema()["type"] == "object"


def test_openai_definitions_derive_from_args_models():
    definitions = openai_tool_definitions(["decide", "get_passage_text"])
    by_name = {d["function"]["name"]: d for d in definitions}
    assert set(by_name["decide"]["function"]["parameters"]["properties"]) == {
        "left_mention_id",
        "right_mention_id",
        "decision",
        "rationale",
    }
    assert "passage_id" in by_name["get_passage_text"]["function"]["parameters"]["properties"]


def test_args_validated_before_execution(db):
    result = run_tool(db, "get_passage_text", {"passage_id": "not-a-uuid"}, ToolContext())
    assert result.is_error
    assert result.payload["error"] == "invalid arguments"
    result = run_tool(db, "nonexistent_tool", {}, ToolContext())
    assert result.is_error


# --- decide: the hard constraints live in the tool (docs/plan.md D5) -------


def test_decide_rejects_conflicting_strong_identifiers(db, env):
    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_liz1.id),
            "right_mention_id": str(env.m_liz2.id),
            "decision": "merge",
            "rationale": "same name, looks like the same person to me",
        },
        ToolContext(),
    )
    assert result.is_error
    assert result.payload["hard_constraint"] == "hard_conflict"
    assert "ssn" in result.payload["conflicting_types"]
    # refused means refused: no decision row, no links
    assert (
        db.execute(
            select(ErDecision).where(
                ErDecision.left_ref["mention_id"].astext == str(env.m_liz1.id)
            )
        ).scalar_one_or_none()
        is None
    )


def test_decide_rejects_name_only_merge(db, env):
    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_name1.id),
            "right_mention_id": str(env.m_name2.id),
            "decision": "merge",
            "rationale": "identical names in two documents",
        },
        ToolContext(),
    )
    assert result.is_error
    assert result.payload["hard_constraint"] == "name_only"


def test_decide_applies_corroborated_merge(db, env):
    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_bob.id),
            "right_mention_id": str(env.m_robert.id),
            "decision": "merge",
            "rationale": "shared exact SSN plus nickname-compatible given names",
        },
        ToolContext(),
    )
    db.commit()
    assert not result.is_error
    assert result.payload["decision"] == "merge"
    person_id = uuid.UUID(result.payload["person_id"])
    links = (
        db.execute(
            select(IdentityLink).where(
                IdentityLink.person_id == person_id, IdentityLink.active.is_(True)
            )
        )
        .scalars()
        .all()
    )
    assert {link.mention_id for link in links} == {env.m_bob.id, env.m_robert.id}
    assert all(link.method == "agent" for link in links)
    decision = db.get(ErDecision, uuid.UUID(result.payload["er_decision_id"]))
    assert decision.decision == "merge"
    assert decision.features["contributions"]["shared_strong"] == pytest.approx(0.90)


def test_decide_bulk_merge_stops_at_approval_gate(db, env):
    run = AgentRunRepository(db).create(
        agent_kind="adjudicator",
        trigger={},
        model="gpt-5.5",
        budget_max_steps=10,
        budget_max_tokens=None,
        budget_max_usd=0.30,
    )
    db.commit()
    env.agent_run_ids.append(run.id)
    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_bulk_anchor.id),
            "right_mention_id": str(env.m_bulk_new.id),
            "decision": "merge",
            "rationale": "shared email joins the spreadsheet cluster",
        },
        ToolContext(agent_run_id=run.id, agent_kind="adjudicator"),
    )
    db.commit()
    assert not result.is_error
    assert result.payload["status"] == "pending_approval"
    assert result.pending_approval_id is not None
    approval = db.get(ApprovalRequest, result.pending_approval_id)
    assert approval.action_type == "bulk_merge"
    assert approval.status == "pending"
    assert approval.payload["impact_mentions"] > 10
    # gate means gate: the newcomer is NOT linked yet
    assert (
        db.execute(
            select(IdentityLink).where(IdentityLink.mention_id == env.m_bulk_new.id)
        ).scalar_one_or_none()
        is None
    )


def test_decide_escalate_creates_review_item(db, env):
    result = run_tool(
        db,
        "decide",
        {
            "left_mention_id": str(env.m_name1.id),
            "right_mention_id": str(env.m_name2.id),
            "decision": "escalate",
            "rationale": "identical names, zero corroborating evidence either way",
        },
        ToolContext(),
    )
    db.commit()
    assert not result.is_error
    item = db.get(ReviewItem, uuid.UUID(result.payload["review_item_id"]))
    assert item.kind == "er_pair"
    assert item.ref["left_mention_id"] == str(env.m_name1.id)


# --- verify_flag: verdicts must re-find their quote verbatim ---------------


def test_verify_flag_rejects_quote_not_in_passage(db, env):
    ctx = ToolContext()
    result = run_tool(
        db,
        "verify_flag",
        {
            "flag_id": str(env.flag.id),
            "verdict": "verified",
            "quoted_span": "THIS TEXT APPEARS NOWHERE IN THE EVIDENCE",
        },
        ctx,
    )
    assert result.is_error
    assert "verbatim" in result.payload["error"]
    assert ctx.verdicts == []  # a refused verdict is not a verdict


def test_verify_flag_accepts_verbatim_quote(db, env):
    ctx = ToolContext()
    result = run_tool(
        db,
        "verify_flag",
        {
            "flag_id": str(env.flag.id),
            "verdict": "verified",
            "quoted_span": "Employee SSN: 523-88-1234",
        },
        ctx,
    )
    assert not result.is_error
    assert result.payload["quote_found"] is True
    assert ctx.verdicts[0]["verdict"] == "verified"


def test_verify_flag_contradiction_lands_in_review_queue(db, env):
    ctx = ToolContext()
    result = run_tool(
        db,
        "verify_flag",
        {
            "flag_id": str(env.flag.id),
            "verdict": "contradicted",
            "quoted_span": "523-88-1234 on file",
            "notes": "value is a placeholder in a template block",
        },
        ctx,
    )
    db.commit()
    assert not result.is_error
    item = db.get(ReviewItem, uuid.UUID(result.payload["review_item_id"]))
    assert item.kind == "flag_audit"
    assert item.ref["exposure_flag_id"] == str(env.flag.id)


# --- investigator forensics on the wrong-extension quarantine --------------


def test_investigator_forensic_tools_round_trip(db, env):
    doc_id = str(env.doc_q.id)
    head = run_tool(db, "get_raw_bytes_head", {"document_id": doc_id, "num_bytes": 8}, ToolContext())
    assert not head.is_error
    assert head.payload["hex"].startswith("50 4b")  # zip magic: the pdf claim is a lie

    sniff = run_tool(db, "sniff_type", {"document_id": doc_id}, ToolContext())
    assert not sniff.is_error
    assert sniff.payload["suggested_file_class"] == "xlsx"

    parsed = run_tool(db, "try_parser", {"document_id": doc_id, "parser": "xlsx"}, ToolContext())
    assert not parsed.is_error
    assert parsed.payload["passage_count"] >= 1

    # a wrong parser fails INFORMATIVELY (docx wants word/document.xml
    # inside the zip; these xlsx bytes don't have it)
    failed = run_tool(db, "try_parser", {"document_id": doc_id, "parser": "docx"}, ToolContext())
    assert failed.is_error
    assert failed.payload["exception"]  # the exact failure is the evidence


def test_resolve_quarantine_reroutes_and_requeues(db, env):
    result = run_tool(
        db,
        "resolve_quarantine",
        {
            "quarantine_id": str(env.quarantine.id),
            "resolution": "bytes are a valid xlsx renamed .pdf; re-route to the xlsx parser",
            "corrected_file_class": "xlsx",
        },
        ToolContext(),
    )
    db.commit()
    assert not result.is_error
    db.refresh(env.quarantine)
    db.refresh(env.doc_q)
    assert env.quarantine.status == "agent_resolved"
    assert env.quarantine.resolution["corrected_file_class"] == "xlsx"
    assert env.doc_q.file_class == "xlsx"
    # resolve_quarantine reprocesses synchronously (docs/plan.md §14b
    # SUSPICIOUS 4 fix) -- the bytes really are a valid xlsx, so the
    # corrected re-route genuinely succeeds all the way to 'done', not
    # left dangling in a non-terminal status for nothing to pick up.
    assert env.doc_q.status == "done"
    assert result.payload["reprocessed_status"] == "done"
    assert db.query(Passage).filter(Passage.document_id == env.doc_q.id).count() > 0
    # already-resolved quarantines refuse a second resolution
    again = run_tool(
        db,
        "resolve_quarantine",
        {"quarantine_id": str(env.quarantine.id), "resolution": "double resolve attempt"},
        ToolContext(),
    )
    assert again.is_error


def test_run_ocr_reads_the_pdf_page(db, env):
    result = run_tool(
        db,
        "run_ocr",
        {"document_id": str(env.doc_pdf.id), "page": 0, "dpi": 200, "preprocess": False},
        ToolContext(),
    )
    assert not result.is_error
    assert result.payload["word_count"] > 0
    assert "OCR" in result.payload["text"].upper()
    out_of_range = run_tool(
        db, "run_ocr", {"document_id": str(env.doc_pdf.id), "page": 9}, ToolContext()
    )
    assert out_of_range.is_error


def test_get_page_image_returns_vision_payload(db, env):
    result = run_tool(
        db, "get_page_image", {"document_id": str(env.doc_pdf.id), "page": 0}, ToolContext()
    )
    assert not result.is_error
    assert result.payload["media_type"] == "image/png"
    assert len(result.payload["_image_base64"]) > 1000
