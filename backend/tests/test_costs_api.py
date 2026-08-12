"""GET /costs/summary + /costs/extrapolation against the real Postgres
(UC2's DB-test convention): summary math over seeded cost_events rows, §9
extrapolation arithmetic (mean cost/doc per file_class x class mix x
scale), and the never-invented-numbers contract (422 on runs without cost
rows). No LLM calls anywhere -- rows are seeded through the repository."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.repositories.cost_events import CostEventRepository
from app.repositories.documents import DocumentRepository
from app.repositories.runs import ProcessingRunRepository


@pytest.fixture()
def db():
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def headers():
    return {"X-API-Key": get_settings().api_key}


def _make_run(db):
    run = ProcessingRunRepository(db).create(config_snapshot={"test": True})
    db.commit()
    return run


def _make_document(db, run, file_class):
    document = DocumentRepository(db).create(
        run_id=run.id,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_filename=f"doc.{file_class}",
        rel_path=f"doc.{file_class}",
        byte_size=1,
        file_class=file_class,
    )
    db.commit()
    return document


@pytest.fixture()
def seeded_run(db):
    """One run: a txt doc with 2 tier-1 calls ($0.004 + $0.006), a
    pdf_digital doc with 1 tier-2 call ($0.03), and one non-document
    (agent) event ($0.05) that must stay out of the per-doc extrapolation."""
    run = _make_run(db)
    txt_doc = _make_document(db, run, "txt")
    pdf_doc = _make_document(db, run, "pdf_digital")
    repo = CostEventRepository(db)
    repo.create(
        run_id=run.id, purpose="tier1", model="DeepSeek-V3.2", document_id=txt_doc.id,
        input_tokens=1000, output_tokens=100, cached_input_tokens=200, cost_usd=0.004,
    )
    repo.create(
        run_id=run.id, purpose="tier1", model="DeepSeek-V3.2", document_id=txt_doc.id,
        input_tokens=1000, output_tokens=100, cached_input_tokens=600, cost_usd=0.006,
    )
    repo.create(
        run_id=run.id, purpose="tier2_text", model="claude-sonnet-4-6", document_id=pdf_doc.id,
        input_tokens=2000, output_tokens=500, cached_input_tokens=1500, cost_usd=0.03,
    )
    repo.create(
        run_id=run.id, purpose="agent_orchestrator", model="claude-opus-5", document_id=None,
        input_tokens=4000, output_tokens=800, cached_input_tokens=0, cost_usd=0.05,
    )
    db.commit()
    return run


def test_summary_math_per_purpose_model(client, headers, seeded_run):
    response = client.get(f"/api/v1/costs/summary?run_id={seeded_run.id}", headers=headers)
    assert response.status_code == 200
    body = response.json()

    rows = {(r["purpose"], r["model"]): r for r in body["rows"]}
    tier1 = rows[("tier1", "DeepSeek-V3.2")]
    assert tier1["calls"] == 2
    assert tier1["input_tokens"] == 2000
    assert tier1["output_tokens"] == 200
    assert tier1["cached_input_tokens"] == 800
    assert tier1["cache_hit_rate"] == pytest.approx(0.4)
    assert tier1["cost_usd"] == pytest.approx(0.01)
    assert tier1["tier"] == "tier1"

    tier2 = rows[("tier2_text", "claude-sonnet-4-6")]
    assert tier2["tier"] == "tier2"
    assert tier2["cache_hit_rate"] == pytest.approx(0.75)

    agent = rows[("agent_orchestrator", "claude-opus-5")]
    assert agent["tier"] == "agents"

    totals = body["totals"]
    assert totals["calls"] == 4
    assert totals["input_tokens"] == 8000
    assert totals["cached_input_tokens"] == 2300
    assert totals["cost_usd"] == pytest.approx(0.09)
    assert totals["cache_hit_rate"] == pytest.approx(2300 / 8000, abs=1e-4)


def test_extrapolation_math_and_sensitivity(client, headers, seeded_run):
    response = client.get(
        f"/api/v1/costs/extrapolation?run_id={seeded_run.id}&scale=1000", headers=headers
    )
    assert response.status_code == 200
    body = response.json()

    # 2 documents, $0.04 attributed to them -> mean $0.02/doc -> $20 at 1000.
    assert body["measured_documents"] == 2
    assert body["measured_document_cost_usd"] == pytest.approx(0.04)
    assert body["mean_cost_per_doc_usd"] == pytest.approx(0.02)
    assert body["extrapolated_cost_usd"] == pytest.approx(20.0)
    # agent cost named separately, never folded into the per-doc mean
    assert body["measured_non_document_cost_usd"] == pytest.approx(0.05)

    by_class = {r["file_class"]: r for r in body["by_class"]}
    assert by_class["txt"]["mean_cost_per_doc_usd"] == pytest.approx(0.01)
    assert by_class["pdf_digital"]["mean_cost_per_doc_usd"] == pytest.approx(0.03)
    assert by_class["txt"]["class_share"] == pytest.approx(0.5)
    assert by_class["pdf_digital"]["cost_at_scale_usd"] == pytest.approx(1000 * 0.5 * 0.03)

    # sensitivity: +10pp into pdf_digital (0.5 -> 0.6), txt scaled to 0.4:
    # 1000 * (0.6*0.03 + 0.4*0.01) = 22.0
    assert body["sensitivity_shifted_class"] == "pdf_digital"
    assert body["sensitivity_extrapolated_cost_usd"] == pytest.approx(22.0)
    assert "class mix" in body["sensitivity_note"]


def test_summary_422_when_run_has_no_cost_events(client, headers, db):
    run = _make_run(db)
    response = client.get(f"/api/v1/costs/summary?run_id={run.id}", headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "validation_failed"


def test_extrapolation_422_without_document_attributed_costs(client, headers, db):
    # A run with ONLY non-document (agent) cost has nothing to extrapolate
    # per-doc from -- 422, never an invented zero table.
    run = _make_run(db)
    _make_document(db, run, "txt")
    CostEventRepository(db).create(
        run_id=run.id, purpose="agent_auditor", model="claude-opus-5",
        document_id=None, cost_usd=0.02,
    )
    db.commit()
    response = client.get(
        f"/api/v1/costs/extrapolation?run_id={run.id}&scale=100", headers=headers
    )
    assert response.status_code == 422


def test_unknown_run_404_and_missing_key_401(client, headers):
    ghost = uuid.uuid4()
    assert client.get(f"/api/v1/costs/summary?run_id={ghost}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/costs/summary?run_id={ghost}").status_code == 401


def test_extrapolation_scale_validation(client, headers, seeded_run):
    response = client.get(
        f"/api/v1/costs/extrapolation?run_id={seeded_run.id}&scale=0", headers=headers
    )
    assert response.status_code == 422  # FastAPI query validation (ge=1)
