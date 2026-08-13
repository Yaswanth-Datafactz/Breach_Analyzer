"""Auto-trigger wiring for the ER + exposure stage (docs/plan.md §14c gap):
after run_processing_run's per-document loop drains, the coordinator calls
run_er_stage + compute_exposure ONCE for the whole run, before marking it
finished, so `POST /runs` means the whole pipeline -- ER and exposure
included -- not four manual steps.

Three behaviors, three tests, all against the real Postgres with FAKE
extraction adapters (or none at all): zero live LLM calls.

1. Parity: a fresh run's auto-computed persons/flags/gray_pairs (recorded
   on run.counters) match what an independent manual run_er_stage +
   compute_exposure call produces -- both stages are documented idempotent,
   so calling them again must reproduce the identical counts.
2. A keyless run (no DEEPSEEK_API_KEY -- extraction skipped, zero mentions)
   still safely auto-triggers: no crash, and the run's counters carry
   EXPLICIT zeros (persons/flags/gray_pairs = 0, er_exposure_status = "ok"),
   never omitted keys -- "ran, found nothing", not "never ran".
3. An ER/exposure failure never un-finishes an otherwise-successful
   parse+extraction run: the run still reaches status='finished', the
   failure is recorded on the counters (er_exposure_status='failed' +
   er_exposure_error), and nothing from the failed attempt is left
   half-persisted (run_er_stage's own writes roll back together with
   compute_exposure's failure).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import ExposureFlag, Person
from app.repositories.runs import ProcessingRunRepository
from app.services import pipeline
from app.services.er.persist import run_er_stage
from app.services.exposure import compute_exposure
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
    db.expire_all()  # the pipeline wrote through its OWN session
    return run.id


def _one_person_payload(name: str, ssn: str) -> dict:
    return {
        "mentions": [{"name_raw": name, "confidence": 1.0}],
        "elements": [
            {"element_type": "name", "value_raw": name, "mention_ref": 0, "confidence": 1.0},
            {"element_type": "ssn", "value_raw": ssn, "mention_ref": 0, "confidence": 0.98},
        ],
    }


def test_auto_trigger_parity_with_manual_call(db, tmp_path: Path, monkeypatch):
    marker = uuid.uuid4().hex
    (tmp_path / "memo.txt").write_bytes(
        f"Note {marker}: Maria Alvarez, SSN 523-41-8722, is affected.".encode()
    )
    tier1 = FakeAdapter([_one_person_payload("Maria Alvarez", "523-41-8722")])
    monkeypatch.setattr(pipeline, "get_tier1_adapter", lambda settings: tier1)
    monkeypatch.setattr(pipeline, "get_tier2_adapter", lambda settings: None)
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "fake-key-for-flag")

    run_id = _run_pipeline_over(db, tmp_path)

    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"
    assert run.counters["mentions"] == 1  # extraction really ran
    assert run.counters["er_exposure_status"] == "ok"
    assert "er_exposure_error" not in run.counters

    # Ground truth the AUTO-TRIGGERED stage actually persisted.
    db_persons = db.scalar(
        select(func.count()).select_from(Person).where(Person.run_id == run_id)
    )
    db_flags = db.scalar(
        select(func.count()).select_from(ExposureFlag).join(Person).where(Person.run_id == run_id)
    )
    assert run.counters["persons"] == db_persons == 1
    assert run.counters["flags"] == db_flags >= 1
    assert run.counters["gray_pairs"] == 0

    # PARITY: an independent manual re-invocation reproduces the identical
    # counts -- both stages are documented idempotent over their own run's
    # prior output (services/er/persist.py, services/exposure.py).
    manual_er = run_er_stage(db, run_id)
    manual_exposure = compute_exposure(db, run_id)
    db.commit()
    assert manual_er.persons == run.counters["persons"]
    assert manual_exposure.flags == run.counters["flags"]
    assert manual_er.gray_items == run.counters["gray_pairs"]


def test_keyless_run_auto_triggers_with_zero_mentions(db, tmp_path: Path, monkeypatch):
    # Force the keyless precondition explicitly rather than assume it of the
    # ambient dev environment -- a real DEEPSEEK_API_KEY in .env (a live-run
    # prerequisite, not a test-hygiene one) must never flip this test's
    # meaning. monkeypatch reverts the env var automatically; the cache
    # still needs clearing on both sides so this test neither reads a
    # pre-existing keyed Settings singleton nor leaves a keyless one behind
    # for whatever runs next (get_settings is a process-wide lru_cache).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.deepseek_api_key == "", "test requires the keyless dev default"
    (tmp_path / "memo.txt").write_bytes(b"Just some ordinary text, no PII markers here.")

    run_id = _run_pipeline_over(db, tmp_path)

    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"  # never crashes, never left non-terminal
    assert run.counters["extracted"] == 0  # extraction itself was skipped
    assert run.counters["mentions"] == 0

    # ER + exposure still ran (never skipped outright) and wrote EXPLICIT
    # zeros -- distinguishable from "the stage never ran" (docs/plan.md
    # §14c: "nothing silently dropped... a keyless run should still safely
    # call ER+exposure").
    assert run.counters["er_exposure_status"] == "ok"
    assert run.counters["persons"] == 0
    assert run.counters["flags"] == 0
    assert run.counters["gray_pairs"] == 0
    assert "er_exposure_error" not in run.counters
    get_settings.cache_clear()  # don't leak the forced-keyless Settings singleton

    assert (
        db.scalar(select(func.count()).select_from(Person).where(Person.run_id == run_id)) == 0
    )


def test_er_exposure_failure_does_not_unfinish_the_run(db, tmp_path: Path, monkeypatch):
    marker = uuid.uuid4().hex
    (tmp_path / "memo.txt").write_bytes(
        f"Note {marker}: Dana Cole, SSN 611-22-3344, is affected.".encode()
    )
    tier1 = FakeAdapter([_one_person_payload("Dana Cole", "611-22-3344")])
    monkeypatch.setattr(pipeline, "get_tier1_adapter", lambda settings: tier1)
    monkeypatch.setattr(pipeline, "get_tier2_adapter", lambda settings: None)
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "fake-key-for-flag")

    def _boom(db_arg, run_id_arg):
        raise RuntimeError("simulated exposure failure")

    # run_er_stage runs for real (writes a real Person row); compute_exposure
    # is the one that blows up -- proves the pair is rolled back TOGETHER,
    # not left half-persisted.
    monkeypatch.setattr(pipeline, "compute_exposure", _boom)

    run_id = _run_pipeline_over(db, tmp_path)

    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"  # parse+extraction succeeded; must not un-finish
    assert run.counters["mentions"] == 1  # extraction itself worked fine
    assert run.counters["er_exposure_status"] == "failed"
    assert "simulated exposure failure" in run.counters["er_exposure_error"]
    # Explicit zeros, and they are TRUE: run_er_stage's persons row does not
    # survive compute_exposure's later failure in the same attempt.
    assert run.counters["persons"] == 0
    assert run.counters["flags"] == 0
    assert (
        db.scalar(select(func.count()).select_from(Person).where(Person.run_id == run_id)) == 0
    )
