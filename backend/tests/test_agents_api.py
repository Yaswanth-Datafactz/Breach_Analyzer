"""/api/v1/agents surface tests (docs/plan.md §5): paged run listing, the
full trace detail payload, the 409-without-key live-dispatch guard (error
envelope, never a crash), the approvals queue, and the decision endpoint
resuming a parked scripted run inline.

Requires the docker-compose Postgres on :5434 with migrations applied.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.services.agents import adjudicator, investigator
from app.services.agents.fake_client import FakeModelClient, ScriptedToolUse, ScriptedTurn
from app.services.agents.runner import AgentRunner
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


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        test_client.headers["X-API-Key"] = get_settings().api_key
        yield test_client


@pytest.fixture()
def keyless(monkeypatch):
    """POST /agents/runs must 409 without a key -- force the condition even
    if the developer's .env happens to carry one."""
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")


def _finished_run(db, env):
    fake = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse("get_document_meta", {"document_id": str(env.doc_q.id)}),
                )
            ),
            ScriptedTurn(text="Inspection complete; no action required."),
        ]
    )
    run = AgentRunner(fake).run(
        db, investigator.DEFINITION, investigator.build_trigger(db, env.quarantine)
    )
    env.agent_run_ids.append(run.id)
    return run


def test_dispatch_without_key_returns_409_envelope(client, keyless):
    response = client.post("/api/v1/agents/runs", json={"kind": "investigator"})
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["type"] == "conflict"
    assert "ANTHROPIC_API_KEY" in body["error"]["message"]
    assert body["error"]["details"]["missing"] == "ANTHROPIC_API_KEY"


def test_dispatch_requires_auth(client):
    response = client.post(
        "/api/v1/agents/runs", json={"kind": "auditor"}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


def test_list_and_detail_serve_the_full_trace(client, db, env):
    run = _finished_run(db, env)

    listing = client.get("/api/v1/agents/runs", params={"kind": "investigator"})
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] >= 1
    assert str(run.id) in [item["id"] for item in page["items"]]

    filtered = client.get("/api/v1/agents/runs", params={"status": "succeeded"})
    assert all(item["status"] == "succeeded" for item in filtered.json()["items"])

    detail = client.get(f"/api/v1/agents/runs/{run.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "succeeded"
    assert len(body["steps"]) == 2
    assert body["steps"][0]["tool_calls"][0]["tool_name"] == "get_document_meta"
    assert body["budget"]["steps_used"] == 2
    assert body["budget"]["max_steps"] == get_settings().investigator_max_steps
    assert body["budget"]["cost_usd"] == pytest.approx(float(run.cost_usd))

    missing = client.get(f"/api/v1/agents/runs/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "not_found"


def test_approval_decision_resumes_parked_scripted_run_inline(client, db, env):
    fake = FakeModelClient(
        [
            ScriptedTurn(
                tool_uses=(
                    ScriptedToolUse(
                        "request_approval",
                        {
                            "action_type": "final_signoff",
                            "payload": {"exposure_rows": 161, "run_id": str(env.run.id)},
                        },
                    ),
                )
            ),
            ScriptedTurn(text="Sign-off recorded; campaign closed."),
        ]
    )
    run = AgentRunner(fake).run(
        db,
        adjudicator.DEFINITION,
        {"left_mention_id": str(env.m_bob.id), "right_mention_id": str(env.m_robert.id)},
    )
    env.agent_run_ids.append(run.id)
    assert run.status == "awaiting_approval"
    approval_id = run.outcome["awaiting_approval"]["approval_request_id"]

    pending = client.get("/api/v1/agents/approvals", params={"status": "pending"})
    assert pending.status_code == 200
    assert approval_id in [item["id"] for item in pending.json()["items"]]

    decision = client.post(
        f"/api/v1/agents/approvals/{approval_id}/decision",
        json={"decision": "approved", "decided_by": "reviewer@test"},
    )
    assert decision.status_code == 200
    body = decision.json()
    assert body["approval"]["status"] == "approved"
    assert body["approval"]["decided_by"] == "reviewer@test"
    assert body["resumed"] is True  # scripted client -> resumed inline
    assert body["run_status"] == "succeeded"

    db.expire_all()
    detail = client.get(f"/api/v1/agents/runs/{run.id}").json()
    assert detail["status"] == "succeeded"

    # deciding twice is a conflict, not a silent overwrite
    again = client.post(
        f"/api/v1/agents/approvals/{approval_id}/decision",
        json={"decision": "rejected", "decided_by": "second@test"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "conflict"


def test_decision_on_unknown_approval_is_404(client):
    response = client.post(
        f"/api/v1/agents/approvals/{uuid.uuid4()}/decision",
        json={"decision": "approved", "decided_by": "reviewer@test"},
    )
    assert response.status_code == 404
