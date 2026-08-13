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
from tests.conftest import build_eml_bytes
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


def test_run_cost_ceiling_stops_extraction_for_later_documents(db, tmp_path: Path, monkeypatch):
    """settings.run_max_cost_usd (core/config.py: hardwired to $20 in
    production, tiny here for a fast/cheap test) is checked after every
    completed document -- once cumulative cost_events spend reaches it,
    extraction flips off for documents not yet dispatched (same
    degrade-gracefully path the no-key test above exercises), while a
    document already processed keeps its real extraction.

    An eml-with-attachment corpus (not N independent top-level files) makes
    this deterministic rather than a same-wave concurrency race: the parent
    eml is the ONLY entry in wave 1 (ingest.queued_ids), so it always
    dispatches before the ceiling can have been crossed; the attachment is
    only discovered and queued into wave 2 AFTER wave 1 fully drains
    (pipeline.py's `while pending:` loop), by which point the post-parent
    ceiling check has already run. Independent same-wave siblings would
    race the semaphore-wakeup against this check with no ordering
    guarantee -- deliberately not the shape used here."""
    marker = uuid.uuid4().hex
    (tmp_path / "records.eml").write_bytes(
        build_eml_bytes(
            body=f"Note {marker}: Maria Alvarez, SSN 523-41-8722, is affected.",
            attachment_name="export.csv",
            attachment_content=f"Name,SSN\nMarker {marker},531-24-8817\n".encode(),
        )
    )

    payload = {
        "mentions": [{"name_raw": "Maria Alvarez", "confidence": 1.0}],
        "elements": [
            {"element_type": "name", "value_raw": "Maria Alvarez", "mention_ref": 0, "confidence": 1.0},
            {"element_type": "ssn", "value_raw": "523-41-8722", "mention_ref": 0, "confidence": 0.98},
        ],
    }
    # Exactly one payload: if the ceiling failed to stop the child's
    # extraction, FakeAdapter's own "exhausted" assertion catches it too --
    # a second safety net beyond this test's explicit assertions below.
    tier1 = FakeAdapter([payload])
    monkeypatch.setattr(pipeline, "get_tier1_adapter", lambda settings: tier1)
    monkeypatch.setattr(pipeline, "get_tier2_adapter", lambda settings: None)
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "fake-key-for-flag")
    # Below one call's real cost ((1000*0.58 + 100*1.68)/1e6 = $0.000748 at
    # DeepSeek-V3.2 pricing) -- the parent always completes (the check only
    # runs AFTER a document finishes), the child is gated by that check.
    monkeypatch.setattr(get_settings(), "run_max_cost_usd", 0.0005)

    run_id = _run_pipeline_over(db, tmp_path)

    documents = db.scalars(select(Document).where(Document.run_id == run_id)).all()
    assert len(documents) == 2
    parent = next(d for d in documents if d.file_class == "eml")
    child = next(d for d in documents if d.file_class == "csv")
    assert parent.status == "done" and child.status == "done"  # terminal either way

    parent_job = db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == parent.id))
    child_job = db.scalar(select(ExtractionJob).where(ExtractionJob.document_id == child.id))
    assert parent_job is not None and float(parent_job.cost_usd) > 0
    assert child_job is None  # ceiling reached before the child was ever dispatched

    run = ProcessingRunRepository(db).get(run_id)
    assert run.counters["tier1_calls"] == 1
    assert run.counters["extracted"] == 1


def test_run_max_cost_usd_defaults_to_20_hardwired_not_none():
    """Locks in the explicit user instruction (2026-08-13): the ceiling
    must be a real default BAKED INTO THE SOURCE, not an opt-in env var
    someone could forget to set before a live keyed run -- asserted
    against the Settings class's own declared field default, not
    get_settings()'s live singleton, which would just reflect whatever
    RUN_MAX_COST_USD happens to be in this machine's .env right now (the
    exact fragility test_no_deepseek_key_skips_extraction_and_finishes_
    at_tier0 above was just fixed for) and could silently stop testing
    the actual code default the moment .env sets its own value."""
    from app.core.config import Settings

    assert Settings.model_fields["run_max_cost_usd"].default == 20.0
