"""/api/v1/accuracy surface tests (docs/plan.md §5): auth (401), 404 on an
unknown processing run / unknown accuracy run, 422 on a processing run
with nothing to score yet and on a missing manifest file, and the real
202 -> (background-scored) -> 200 round trip with real metrics (task 5's
"accuracy API (401/404/422/200 shapes)"). FastAPI's TestClient executes
BackgroundTasks synchronously before `client.post()` returns (verified
empirically against this Starlette version), so the 202 response's
background scoring has already completed by the time the follow-up GET
runs -- no polling needed.

Requires the docker-compose Postgres on :5434 with migrations applied. No
LLM calls anywhere.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import ProcessingRun
from app.db.session import SessionLocal
from app.main import app
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.mentions import MentionRepository
from app.repositories.passages import PassageRepository
from app.repositories.persons import PersonRepository
from app.repositories.pii_elements import PiiElementRepository
from app.repositories.runs import ProcessingRunRepository
from app.services.er.normalize import normalize_value


@pytest.fixture()
def db():
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


def _cleanup_run(db, run_id) -> None:
    db.rollback()
    db.execute(delete(ProcessingRun).where(ProcessingRun.id == run_id))
    db.commit()


def _sha() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _scored_run(db):
    """One run, one document, one cleanly-matchable person with an ssn
    flag -- just enough for the accuracy endpoint to have something real
    to score (the arithmetic itself is covered by test_accuracy_scoring_
    db.py; this file is about the API's wiring and status codes)."""
    from app.repositories.documents import DocumentRepository

    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.flush()
    document = DocumentRepository(db).create(
        run_id=run.id, sha256=_sha(), original_filename="D_api.pdf", rel_path="D_api.pdf",
        byte_size=10, file_class="pdf_digital", source_kind="corpus",
    )
    passage = PassageRepository(db).create(
        document_id=document.id, seq=0, kind="page", locator={"page": 1},
        text="Priya Chandra, SSN 123-45-6789", ocr=False,
    )
    mention = MentionRepository(db).create(
        document_id=document.id, passage_id=passage.id, name_raw="Priya Chandra",
        detector="llm_tier1", confidence=0.9,
    )
    element = PiiElementRepository(db).create(
        document_id=document.id, passage_id=passage.id, element_type="ssn",
        value_raw="123-45-6789", value_normalized=normalize_value("ssn", "123-45-6789"),
        char_start=0, char_end=11, detector="llm_tier1", validation_status="valid",
        mention_id=mention.id, confidence=0.9,
    )
    person = PersonRepository(db).create(run_id=run.id, best_name="Priya Chandra", mention_count=1, document_count=1)
    IdentityLinkRepository(db).create(person_id=person.id, mention_id=mention.id, method="rule", score=1.0, rule_id="test")
    flag_repo = ExposureFlagRepository(db)
    flag = flag_repo.create_flag(person_id=person.id, category="ssn", exposed=True, confidence=0.9)
    flag_repo.add_evidence(
        exposure_flag_id=flag.id, pii_element_id=element.id, document_id=document.id,
        passage_id=passage.id, snippet="...",
    )
    db.commit()
    return run


def _write_manifest(tmp_path):
    manifest = {
        "seed": 1,
        "profile": "test",
        "identities": [
            {
                "person_uid": "P_API",
                "canonical_name": "Priya Chandra",
                "dob": "1988-02-02",
                "name_variants": [],
                "elements": {"ssn": "123-45-6789"},
            }
        ],
        "documents": [
            {
                "filename": "D_api.pdf",
                "plantings": [
                    {"person_uid": "P_API", "element_type": "name", "value": "Priya Chandra"},
                    {"person_uid": "P_API", "element_type": "ssn", "value": "123-45-6789"},
                ],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_missing_key_401(client):
    assert client.get("/api/v1/accuracy/runs").status_code == 401
    assert client.get(f"/api/v1/accuracy/runs/{uuid.uuid4()}").status_code == 401
    assert client.post("/api/v1/accuracy/runs", json={"processing_run_id": str(uuid.uuid4())}).status_code == 401


# ---------------------------------------------------------------------------
# POST /accuracy/runs prerequisite validation
# ---------------------------------------------------------------------------


def test_create_404_unknown_processing_run(client, headers):
    response = client.post(
        "/api/v1/accuracy/runs",
        json={"processing_run_id": str(uuid.uuid4()), "manifest_path": "data/manifest-mini.json"},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"


def test_create_422_when_run_has_no_persons_yet(client, headers, db):
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        response = client.post(
            "/api/v1/accuracy/runs",
            json={"processing_run_id": str(run.id), "manifest_path": "data/manifest-mini.json"},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "validation_failed"
    finally:
        _cleanup_run(db, run.id)


def test_create_422_when_manifest_path_missing(client, headers, db):
    run = _scored_run(db)
    try:
        response = client.post(
            "/api/v1/accuracy/runs",
            json={"processing_run_id": str(run.id), "manifest_path": "data/does-not-exist-manifest.json"},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "validation_failed"
    finally:
        _cleanup_run(db, run.id)


# ---------------------------------------------------------------------------
# The real 202 -> scored -> 200 round trip
# ---------------------------------------------------------------------------


def test_create_202_scores_in_background_and_get_returns_finished_metrics(client, headers, db, tmp_path):
    run = _scored_run(db)
    manifest_path = _write_manifest(tmp_path)
    try:
        create_response = client.post(
            "/api/v1/accuracy/runs",
            json={
                "processing_run_id": str(run.id),
                "manifest_path": str(manifest_path),
                "config_profile": "measured",
            },
            headers=headers,
        )
        assert create_response.status_code == 202
        body = create_response.json()
        accuracy_run_id = body["id"]
        assert body["status"] in ("pending", "running", "finished")  # background task may already be done

        get_response = client.get(f"/api/v1/accuracy/runs/{accuracy_run_id}", headers=headers)
        assert get_response.status_code == 200
        detail = get_response.json()
        assert detail["status"] == "finished"
        assert detail["config_snapshot"]["config_profile"] == "measured"
        assert detail["config_snapshot"]["manifest_profile"] == "test"
        assert detail["config_snapshot"]["processing_run_id"] == str(run.id)

        metrics = detail["metrics"]
        assert metrics is not None
        assert metrics["person"]["matched"] == 1
        assert metrics["person"]["manifest_identities"] == 1
        assert metrics["wrongly_merged_manifest_identities"] == 0
        category_by_name = {row["category"]: row for row in metrics["per_category"]}
        assert category_by_name["ssn"]["tp"] == 1

        list_response = client.get("/api/v1/accuracy/runs", headers=headers)
        assert list_response.status_code == 200
        list_body = list_response.json()
        assert any(row["id"] == accuracy_run_id for row in list_body["items"])
        summary_row = next(row for row in list_body["items"] if row["id"] == accuracy_run_id)
        assert summary_row["status"] == "finished"
        assert summary_row["person_precision"] == 1.0
    finally:
        _cleanup_run(db, run.id)


def test_get_accuracy_run_404_unknown_id(client, headers):
    response = client.get(f"/api/v1/accuracy/runs/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found"
