"""AgentRunner tests over the scripted FakeModelClient (docs/plan.md §3's
AgentRunner mechanics -- zero live tokens): full loops for all four agent
kinds, budget parking with a partial trace, kill-simulation (trace survives
a handler crash), approval park + resume, the directives applier's menu
refusal, and cost/token accounting against the MODEL_PRICES table.

Requires the docker-compose Postgres on :5434 with migrations applied.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import select

from app.core.config import MODEL_PRICES_USD_PER_MTOK
from app.db.models import AgentStep, AgentToolCall, CostEvent, ErDecision, IdentityLink
from app.db.session import SessionLocal
from app.repositories.approvals import ApprovalRepository
from app.services.agents import adjudicator, auditor, investigator, orchestrator
from app.services.agents.budgets import Budget
from app.services.agents.fake_client import FakeModelClient, ScriptedToolUse, ScriptedTurn
from app.services.agents.runner import AgentRunner, is_parked, resume_parked
from app.services.agents.tools import REGISTRY
from tests.agent_env import build_agent_env, teardown_agent_env


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


def _steps(db, run_id):
    return (
        db.execute(
            select(AgentStep).where(AgentStep.agent_run_id == run_id).order_by(AgentStep.step_no)
        )
        .scalars()
        .all()
    )


def _tool_calls(db, run_id):
    return (
        db.execute(
            select(AgentToolCall)
            .join(AgentStep, AgentToolCall.agent_step_id == AgentStep.id)
            .where(AgentStep.agent_run_id == run_id)
        )
        .scalars()
        .all()
    )


def test_investigator_full_loop_resolves_quarantine(db, env):
    doc_id = str(env.doc_q.id)
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse("get_document_meta", {"document_id": doc_id}),
                    ScriptedToolUse("sniff_type", {"document_id": doc_id}),
                )
            ),
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse("try_parser", {"document_id": doc_id, "parser": "xlsx"}),
                )
            ),
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "resolve_quarantine",
                        {
                            "quarantine_id": str(env.quarantine.id),
                            "resolution": "sniff + try_parser prove xlsx bytes under a .pdf name",
                            "corrected_file_class": "xlsx",
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Resolved: renamed spreadsheet re-routed to the xlsx parser."),
        ]
    )
    run = investigator.investigate_quarantine(db, client, env.quarantine.id)
    env.agent_run_ids.append(run.id)

    assert run.status == "succeeded"
    assert run.outcome["resolved"] is True
    assert run.outcome["resolution"]["corrected_file_class"] == "xlsx"
    db.refresh(env.quarantine)
    assert env.quarantine.status == "agent_resolved"
    assert env.quarantine.resolved_by_agent_run_id == run.id
    db.refresh(env.doc_q)
    assert env.doc_q.status == "queued"

    steps = _steps(db, run.id)
    assert [s.step_no for s in steps] == [1, 2, 3, 4]
    assert steps[0].stop_reason == "tool_use"
    assert steps[-1].stop_reason == "end_turn"
    calls = _tool_calls(db, run.id)
    assert sorted(c.tool_name for c in calls) == sorted(
        ["get_document_meta", "sniff_type", "try_parser", "resolve_quarantine"]
    )
    assert all(not c.is_error for c in calls)

    # accounting: step rollups == run rollups == MODEL_PRICES arithmetic
    assert run.steps_used == 4
    assert run.tokens_in == sum(s.tokens_in for s in steps) == 4 * 500
    assert run.tokens_out == sum(s.tokens_out for s in steps) == 4 * 80
    prices = MODEL_PRICES_USD_PER_MTOK[run.model]
    expected = 4 * round((500 * prices["input"] + 80 * prices["output"]) / 1_000_000, 6)
    assert float(run.cost_usd) == pytest.approx(expected)
    assert float(sum(s.cost_usd for s in steps)) == pytest.approx(expected)
    cost_events = (
        db.execute(select(CostEvent).where(CostEvent.agent_run_id == run.id)).scalars().all()
    )
    assert len(cost_events) == 4
    assert all(e.purpose == "agent_investigator" for e in cost_events)
    assert all(e.run_id == env.run.id for e in cost_events)


def test_budget_max_steps_halts_mid_plan_with_partial_trace(db, env):
    doc_id = str(env.doc_q.id)
    probe = ScriptedToolUse("get_document_meta", {"document_id": doc_id})
    client = FakeModelClient(
        [
            ScriptedTurn(tool_uses=(probe,)),
            ScriptedTurn(tool_uses=(probe,)),
            ScriptedTurn(tool_uses=(probe,)),  # never reached: budget parks first
            ScriptedTurn(text="never reached"),
        ]
    )
    trigger = investigator.build_trigger(db, env.quarantine)
    run = AgentRunner(client).run(
        db, investigator.DEFINITION, trigger, budget=Budget(max_steps=2)
    )
    env.agent_run_ids.append(run.id)

    assert run.status == "budget_exceeded"
    assert run.steps_used == 2
    assert run.outcome["partial"] is True
    assert run.outcome["budget_exceeded"] == "steps"
    assert len(_steps(db, run.id)) == 2  # the partial trace IS persisted
    assert len(_tool_calls(db, run.id)) == 2
    assert len(client.turns) == 2  # the model was never asked for turn 3


def test_kill_simulation_leaves_steps_and_tool_calls(db, env, monkeypatch):
    spec = REGISTRY["get_document_meta"]

    def detonate(session, args, ctx):
        raise RuntimeError("simulated crash inside the handler")

    monkeypatch.setitem(REGISTRY, "get_document_meta", dataclasses.replace(spec, handler=detonate))
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse("get_document_meta", {"document_id": str(env.doc_q.id)}),
                )
            ),
            ScriptedTurn(text="never reached"),
        ]
    )
    run = AgentRunner(client).run(
        db, investigator.DEFINITION, investigator.build_trigger(db, env.quarantine)
    )
    env.agent_run_ids.append(run.id)

    assert run.status == "failed"
    assert "crashed" in run.outcome["error"]
    steps = _steps(db, run.id)
    calls = _tool_calls(db, run.id)
    assert len(steps) == 1  # the step row was committed BEFORE the tool ran
    assert len(calls) == 1  # ... and the tool row (with args) before the handler
    assert calls[0].args == {"document_id": str(env.doc_q.id)}
    assert calls[0].is_error
    assert "RuntimeError" in calls[0].result_summary["error"]


def test_orchestrator_directives_validated_and_applied(db, env):
    run_id = str(env.run.id)
    client = FakeModelClient(
        [
            ScriptedTurn(tool_uses=(ScriptedToolUse("get_run_stats", {"run_id": run_id}),)),
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "set_batch_priority",
                        {
                            "file_class": "pdf_scanned",
                            "priority": 8,
                            "reason": "OCR-heavy class is the throughput bottleneck",
                        },
                    ),
                    ScriptedToolUse(
                        "set_escalation_threshold",
                        {"threshold": 0.7, "reason": "tier-2 spend is under budget"},
                    ),
                )
            ),
            ScriptedTurn(text="Run healthy; reprioritized scanned PDFs and widened escalation."),
        ]
    )
    run = AgentRunner(client).run(
        db, orchestrator.DEFINITION, {"run_id": run_id, "checkpoint": "mid_run"}
    )
    env.agent_run_ids.append(run.id)

    assert run.status == "succeeded"
    assert len(run.outcome["directives"]) == 2
    assert run.outcome["refused"] == []
    assert run.outcome["campaign"]["batch_priorities"] == {"pdf_scanned": 8}
    assert run.outcome["campaign"]["escalation_threshold"] == 0.7
    # the pipeline-facing read model reflects the applied campaign
    state = orchestrator.latest_campaign_state(db, env.run.id)
    assert state["batch_priorities"] == {"pdf_scanned": 8}
    assert state["escalation_threshold"] == 0.7


def test_directives_applier_refuses_out_of_menu_actions(db):
    application = orchestrator.apply_directives(
        db,
        [
            {"action": "delete_documents", "params": {"document_id": "x"}},  # not in menu
            {
                "action": "set_batch_priority",  # in menu, invalid params
                "params": {"file_class": "exe", "priority": 5, "reason": "nope"},
            },
            {
                "action": "set_escalation_threshold",  # valid
                "params": {"threshold": 0.65, "reason": "measured tier-2 headroom"},
            },
        ],
    )
    assert len(application["refused"]) == 2
    assert len(application["applied"]) == 1
    assert application["campaign"]["escalation_threshold"] == 0.65
    assert application["campaign"]["batch_priorities"] == {}


def test_adjudicator_full_loop_records_decision(db, env):
    left, right = str(env.m_liz1.id), str(env.m_liz2.id)
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "compare_features", {"left_mention_id": left, "right_mention_id": right}
                    ),
                )
            ),
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "decide",
                        {
                            "left_mention_id": left,
                            "right_mention_id": right,
                            "decision": "no_merge",
                            "rationale": "conflicting SSNs: same name, different people",
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Distinct people: the SSN conflict is decisive."),
        ]
    )
    run = adjudicator.adjudicate_pair(db, client, env.m_liz1.id, env.m_liz2.id, score=0.35)
    env.agent_run_ids.append(run.id)

    assert run.status == "succeeded"
    assert run.outcome["decision"]["decision"] == "no_merge"
    decision = db.get(ErDecision, uuid.UUID(run.outcome["decision"]["er_decision_id"]))
    assert decision.method == "agent"
    assert decision.agent_run_id == run.id
    assert decision.features["conflicting_strong_types"] == ["ssn"]


def test_bulk_merge_parks_then_resume_applies(db, env):
    left, right = str(env.m_bulk_anchor.id), str(env.m_bulk_new.id)
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "decide",
                        {
                            "left_mention_id": left,
                            "right_mention_id": right,
                            "decision": "merge",
                            "rationale": "shared email joins the spreadsheet cluster",
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Merge applied after human approval."),
        ]
    )
    run = adjudicator.adjudicate_pair(db, client, env.m_bulk_anchor.id, env.m_bulk_new.id)
    env.agent_run_ids.append(run.id)

    assert run.status == "awaiting_approval"
    assert is_parked(run.id)
    approval_id = uuid.UUID(run.outcome["awaiting_approval"]["approval_request_id"])
    approvals = ApprovalRepository(db)
    approval = approvals.get(approval_id)
    assert approval.action_type == "bulk_merge"
    assert len(client.turns) == 1  # the loop is parked, not finished

    approvals.decide(approval, status="approved", decided_by="reviewer@test")
    db.commit()
    resumed = resume_parked(db, run.id, decided_by="reviewer@test")

    assert resumed is not None
    assert resumed.status == "succeeded"
    assert not is_parked(run.id)
    link = db.execute(
        select(IdentityLink).where(
            IdentityLink.mention_id == env.m_bulk_new.id, IdentityLink.active.is_(True)
        )
    ).scalar_one()
    assert link.person_id == env.person_bulk.id  # the approved merge was APPLIED
    decision = db.execute(
        select(ErDecision).where(ErDecision.approval_request_id == approval_id)
    ).scalar_one()
    assert decision.decision == "merge"
    # resuming a run that is not parked is a clean None, not a crash
    assert resume_parked(db, run.id, decided_by="reviewer@test") is None


def test_auditor_full_loop_verifies_and_estimates(db, env):
    flag_id = str(env.flag.id)
    # 3 steps/flag x 1 flag = a 3-step budget (plan §3) -- the script packs
    # parallel tool calls into single turns exactly like a budget-aware
    # auditor must.
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse("get_flag_with_evidence", {"flag_id": flag_id}),
                    ScriptedToolUse("get_passage_text", {"passage_id": str(env.passage_flag.id)}),
                )
            ),
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "verify_flag",
                        {
                            "flag_id": flag_id,
                            "verdict": "verified",
                            "quoted_span": "Employee SSN: 523-88-1234",
                        },
                    ),
                    ScriptedToolUse(
                        "report_estimate",
                        {
                            "sample_size": 1,
                            "verified": 1,
                            "contradicted": 0,
                            "estimated_error_rate": 0.0,
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Sampled flag verified against its passage; no contradictions."),
        ]
    )
    run = auditor.audit_flags(db, client, [env.flag.id])
    env.agent_run_ids.append(run.id)

    assert run.status == "succeeded"
    assert run.outcome["verdicts"][0]["verdict"] == "verified"
    assert run.outcome["contradicted"] == 0
    assert run.outcome["estimate"]["estimated_error_rate"] == 0.0
    assert run.budget_max_steps == 3  # 3 steps/flag x 1 flag (plan §3)


def test_auditor_verdict_with_bad_quote_is_rejected_at_tool_level(db, env):
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "verify_flag",
                        {
                            "flag_id": str(env.flag.id),
                            "verdict": "contradicted",
                            "quoted_span": "A SPAN THE PASSAGE NEVER CONTAINED",
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Could not ground a verdict; stopping."),
        ]
    )
    run = auditor.audit_flags(db, client, [env.flag.id])
    env.agent_run_ids.append(run.id)

    assert run.status == "succeeded"
    assert run.outcome["verdicts"] == []  # the refused verdict never registered
    calls = _tool_calls(db, run.id)
    assert len(calls) == 1 and calls[0].is_error


def test_escalation_is_terminal_and_marks_quarantine(db, env):
    # A fresh open quarantine on doc_b so the escalate path has a target.
    from app.db.models import Quarantine

    quarantine = Quarantine(
        document_id=env.doc_b.id,
        stage="ingest",
        reason_code="password_protected",
        detail="pikepdf.PasswordError: encrypted",
    )
    db.add(quarantine)
    db.commit()
    client = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "escalate",
                        {
                            "reason": "password-protected PDF, no credentials available",
                            "diagnosis": "pikepdf refuses without the user password; "
                            "bytes confirm AES-256 encryption",
                        },
                    ),
                )
            ),
            ScriptedTurn(text="never reached: escalation ends the run"),
        ]
    )
    run = investigator.investigate_quarantine(db, client, quarantine.id)
    env.agent_run_ids.append(run.id)

    assert run.status == "escalated"
    assert run.outcome["escalation"]["reason"].startswith("password-protected")
    db.refresh(quarantine)
    assert quarantine.status == "escalated"
    assert len(client.turns) == 1  # terminal: the second scripted turn never ran
