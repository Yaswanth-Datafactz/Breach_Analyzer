"""Integration test: the full B1 pipeline over data/corpus-mini (60 docs,
seed 42, zero LLM calls -- everything below tier 1 is deterministic).

Asserts the phase gate from docs/plan.md §15: reconciliation invariant
(every document `done` or quarantined-with-reason), passages present for
every parsed document, and quarantine reasons consistent with the mini
manifest's problem entries (the mini corpus is digital-only at the time of
writing, so the expected-problem set may be empty -- the assertion is
written against whatever the manifest declares, not against a snapshot of
it). Finishes with an authenticated API round-trip over the fresh data:
run counters, passages drill-down, single passage, original file bytes.

Requires the docker-compose Postgres on :5434 with migrations applied,
and data/corpus-mini + data/manifest-mini.json (CLAUDE.md's corpusgen
command). Skips cleanly if the corpus has not been generated.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Document, Quarantine
from app.db.session import SessionLocal
from app.main import app
from app.repositories.runs import ProcessingRunRepository
from app.services.pipeline import build_config_snapshot, run_processing_run

_REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = _REPO_ROOT / "data" / "corpus-mini"
MANIFEST_PATH = _REPO_ROOT / "data" / "manifest-mini.json"

pytestmark = pytest.mark.skipif(
    not (CORPUS_DIR.is_dir() and MANIFEST_PATH.is_file()),
    reason="mini corpus not generated (see CLAUDE.md corpusgen command)",
)


@pytest.fixture(scope="module")
def mini_run_id():
    """Run the pipeline over the mini corpus once for the whole module.

    Prior runs over this corpus are left alone: migration 002 scopes
    documents.sha256 uniqueness per-run, so a fresh ingest can no longer be
    shadowed by (or shadow) an earlier run's rows -- including demo/seed
    data a live session may have built on top of a corpus-mini run. This
    fixture used to delete every prior ProcessingRun over data/corpus-mini
    before re-ingesting; that silently destroyed demo state (docs/plan.md
    §14b SUSPICIOUS 1) and is no longer necessary post-002. Only
    free_sha_collisions runs, to clear any *fixture-bytes* collision under
    a non-protected (non data/) corpus_path -- it is a no-op against real
    corpus-mini/corpus runs by construction."""
    db = SessionLocal()
    try:
        from tests.conftest import free_sha_collisions

        free_sha_collisions(db, [p.read_bytes() for p in sorted(CORPUS_DIR.iterdir()) if p.is_file()])
        db.commit()

        run = ProcessingRunRepository(db).create(
            config_snapshot=build_config_snapshot(corpus_path=str(CORPUS_DIR))
        )
        db.commit()
        asyncio.run(run_processing_run(run.id))
        yield run.id
    finally:
        db.close()


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_run_finishes_and_reconciles(db, mini_run_id):
    run = ProcessingRunRepository(db).get(mini_run_id)
    assert run.status == "finished"
    assert run.started_at is not None and run.finished_at is not None

    documents = list(db.scalars(select(Document).where(Document.run_id == mini_run_id)))
    corpus_files = [p for p in CORPUS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    assert len(documents) >= len(corpus_files) - (run.counters.get("deduped") or 0)

    # THE invariant (CLAUDE.md "nothing silently dropped"): every document
    # terminal, and every non-done document has a quarantine row.
    assert all(d.status in ("done", "quarantined") for d in documents)
    for document in documents:
        if document.status == "quarantined":
            quarantine = db.scalar(
                select(Quarantine).where(Quarantine.document_id == document.id)
            )
            assert quarantine is not None, f"{document.rel_path} quarantined without a reason row"

    # Counters reconcile with reality.
    assert run.counters["ingested"] == len(documents)
    assert run.counters["done"] + run.counters["quarantined"] == len(documents)
    assert run.counters["failed"] == 0


def test_every_parsed_document_has_passages(db, mini_run_id):
    documents = list(db.scalars(select(Document).where(Document.run_id == mini_run_id)))
    done = [d for d in documents if d.status == "done"]
    assert done, "mini corpus produced no parsed documents"
    for document in done:
        assert len(document.passages) > 0, f"{document.rel_path} parsed but has no passages"


def test_quarantines_match_manifest_problem_entries(db, mini_run_id):
    manifest = json.loads(MANIFEST_PATH.read_text())
    expected_problem_files = {
        d["filename"]
        for d in manifest["documents"]
        if d.get("problem")
        # Wrong-extension problems are deterministically RECOVERED
        # (classify.py reclassifies by sniff), so they must NOT appear
        # in the quarantine set.
        and "wrong_ext" not in str(d["problem"])
    }

    rows = db.execute(
        select(Document, Quarantine)
        .join(Quarantine, Quarantine.document_id == Document.id)
        .where(Document.run_id == mini_run_id)
    ).all()
    quarantined_files = {document.original_filename for document, _ in rows}

    assert quarantined_files == expected_problem_files


def test_api_round_trip_over_fresh_run(db, mini_run_id):
    client = TestClient(app)
    headers = {"X-API-Key": get_settings().api_key}

    run_response = client.get(f"/api/v1/runs/{mini_run_id}", headers=headers)
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "finished"

    quarantines_response = client.get(
        f"/api/v1/runs/{mini_run_id}/quarantines", headers=headers
    )
    assert quarantines_response.status_code == 200

    document = db.scalar(
        select(Document).where(Document.run_id == mini_run_id, Document.status == "done").limit(1)
    )
    passages_response = client.get(
        f"/api/v1/documents/{document.id}/passages", headers=headers
    )
    assert passages_response.status_code == 200
    passages = passages_response.json()
    assert passages["total"] > 0
    first = passages["items"][0]
    assert first["locator"]

    passage_response = client.get(f"/api/v1/passages/{first['id']}", headers=headers)
    assert passage_response.status_code == 200
    assert passage_response.json()["text"] == first["text"]

    file_response = client.get(f"/api/v1/documents/{document.id}/file", headers=headers)
    assert file_response.status_code == 200
    assert file_response.content == (CORPUS_DIR / document.rel_path).read_bytes()

    # Auth is enforced on every route (docs/plan.md §11).
    assert client.get(f"/api/v1/runs/{mini_run_id}").status_code == 401
