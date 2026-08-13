"""Pipeline wiring of the B2 extraction stage, against the real Postgres:
(a) the degrade-gracefully contract -- no DEEPSEEK_API_KEY means the stage
is skipped and documents finish `done` at tier-0 depth with no
extraction_jobs row and no cost_events; (b) the enabled path with FAKE
adapters monkeypatched in -- extraction_jobs row with tier_path + rollups,
document status flows parsed -> extracted -> done, run counters carry the
extraction numbers. Zero live LLM calls either way."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import CostEvent, Document, ExtractionJob, Mention
from app.repositories.runs import ProcessingRunRepository
from app.services import pipeline
from app.services.pipeline import build_config_snapshot, run_processing_run
from tests.test_extraction_service import FakeAdapter


@pytest.fixture()
def db():
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _run_pipeline_over(db, corpus_dir: Path) -> uuid.UUID:
    run = ProcessingRunRepository(db).create(
        config_snapshot=build_config_snapshot(corpus_path=str(corpus_dir))
    )
    db.commit()
    asyncio.run(run_processing_run(run.id))
    db.expire_all()
    return run.id


def _document(db, run_id) -> Document:
    return db.scalar(select(Document).where(Document.run_id == run_id))


def test_no_deepseek_key_skips_extraction_and_finishes_at_tier0(db, tmp_path: Path, monkeypatch):
    # Force the keyless precondition explicitly rather than assume it of the
    # ambient dev environment -- a real DEEPSEEK_API_KEY in .env (a live-run
    # prerequisite, not a test-hygiene one) must never flip this test's
    # meaning (mirrors test_pipeline_er_exposure_stage.py's identical fix).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.deepseek_api_key == "", "test requires the keyless dev default"
    marker = uuid.uuid4().hex
    (tmp_path / "memo.txt").write_bytes(
        f"Maria Alvarez, SSN 523-41-8722, ref {marker}.".encode()
    )

    run_id = _run_pipeline_over(db, tmp_path)

    document = _document(db, run_id)
    assert document.status == "done"  # tier-0 depth, still terminal
    assert db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == document.id)) is None
    assert db.scalars(select(CostEvent).where(CostEvent.run_id == run_id)).all() == []
    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"
    assert run.counters["extracted"] == 0
    assert run.counters["tier1_calls"] == 0
    # tier-0 still ran
    assert run.counters["tier0_elements"] >= 1
    get_settings.cache_clear()  # don't leak the forced-keyless Settings singleton


def test_enabled_path_writes_job_row_and_counters(db, tmp_path: Path, monkeypatch):
    marker = uuid.uuid4().hex
    (tmp_path / "memo.txt").write_bytes(
        f"Note {marker}: Maria Alvarez, SSN 523-41-8722, is affected.".encode()
    )

    payload = {
        "mentions": [{"name_raw": "Maria Alvarez", "confidence": 1.0}],
        "elements": [
            {"element_type": "name", "value_raw": "Maria Alvarez", "mention_ref": 0, "confidence": 1.0},
            {"element_type": "ssn", "value_raw": "523-41-8722", "mention_ref": 0, "confidence": 0.98},
        ],
    }
    tier1 = FakeAdapter([payload])
    monkeypatch.setattr(pipeline, "get_tier1_adapter", lambda settings: tier1)
    monkeypatch.setattr(pipeline, "get_tier2_adapter", lambda settings: None)
    # flag only -- the fake adapter never reads it (config-only keyed run)
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "fake-key-for-flag")

    run_id = _run_pipeline_over(db, tmp_path)

    document = _document(db, run_id)
    assert document.status == "done"
    job = db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == document.id))
    assert job is not None and job.status == "done"
    assert job.model == get_settings().deepseek_model
    assert job.tier_path and job.tier_path[0]["tier"] == 1
    assert job.input_tokens == 1000 and job.output_tokens == 100
    assert float(job.cost_usd) > 0

    events = db.scalars(select(CostEvent).where(CostEvent.run_id == run_id)).all()
    assert len(events) == 1 and events[0].purpose == "tier1"

    mention = db.scalar(select(Mention).where(Mention.document_id == document.id))
    assert mention is not None and mention.detector == "llm_tier1"

    run = ProcessingRunRepository(db).get(run_id)
    assert run.counters["extracted"] == 1
    assert run.counters["mentions"] == 1
    assert run.counters["llm_elements"] == 2
    assert run.counters["tier1_calls"] == 1
    assert run.counters["tier2_calls"] == 0
