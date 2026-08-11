"""Pipeline behavior tests against the real Postgres (UC2's convention for
DB-touching tests -- docker-compose :5434, migrations applied): sha256
dedup, eml attachment recursion, and the handcrafted problem corpus
(fixtures built in conftest.py, independent of the corpusgen track).

Test contents embed a per-session uuid where dedup must NOT fire across
pytest sessions (documents.sha256 is globally UNIQUE); the problem corpus
instead frees colliding rows first, because bytes like the zero-byte file
hash identically every session by definition.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Document, Quarantine
from app.db.session import SessionLocal
from app.repositories.runs import ProcessingRunRepository
from app.services.pipeline import build_config_snapshot, run_processing_run
from tests.conftest import build_eml_bytes, free_sha_collisions


@pytest.fixture()
def db():
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


def _documents_for_run(db, run_id: uuid.UUID) -> list[Document]:
    return list(db.scalars(select(Document).where(Document.run_id == run_id)))


def test_sha256_dedup_parses_identical_content_once(db, tmp_path: Path):
    marker = uuid.uuid4().hex  # unique per session: dedup must fire WITHIN the run only
    content = f"Name,SSN\nCasey Vance,531-24-8817\nmarker,{marker}\n".encode()
    (tmp_path / "a_export.csv").write_bytes(content)
    (tmp_path / "b_copy_of_export.csv").write_bytes(content)
    (tmp_path / "c_unique.txt").write_bytes(f"unique {marker}".encode())

    run_id = _run_pipeline_over(db, tmp_path)

    documents = _documents_for_run(db, run_id)
    # 3 files seen, but the identical pair produced ONE documents row.
    assert len(documents) == 2
    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"
    assert run.counters["files_seen"] == 3
    assert run.counters["ingested"] == 2
    assert run.counters["deduped"] == 1
    assert all(d.status == "done" for d in documents)


def test_eml_attachment_recurses_into_child_document(db, tmp_path: Path):
    marker = uuid.uuid4().hex
    attachment_content = f"Name,SSN\nMarker {marker},531-24-8817\n".encode()
    (tmp_path / "records.eml").write_bytes(
        build_eml_bytes(
            body=f"attached, ref {marker}",
            attachment_name="export.csv",
            attachment_content=attachment_content,
        )
    )

    run_id = _run_pipeline_over(db, tmp_path)

    documents = _documents_for_run(db, run_id)
    assert len(documents) == 2
    parent = next(d for d in documents if d.file_class == "eml")
    child = next(d for d in documents if d.file_class == "csv")

    assert parent.source_kind == "corpus"
    assert parent.status == "done"
    assert child.source_kind == "attachment"
    assert child.parent_document_id == parent.id
    assert child.original_filename == "export.csv"
    assert child.rel_path == "records.eml!export.csv"
    assert child.status == "done"
    assert len(child.passages) > 0
    assert any(f"Marker {marker}" in p.text for p in child.passages)

    run = ProcessingRunRepository(db).get(run_id)
    assert run.counters["attachments"] == 1


def test_problem_corpus_quarantine_reasons(db, tmp_path: Path, problem_files: dict[str, Path]):
    corpus = tmp_path / "problems"
    corpus.mkdir()
    contents = {}
    for name, path in problem_files.items():
        contents[name] = path.read_bytes()
        (corpus / name).write_bytes(contents[name])
    # One healthy file so the run also exercises the happy path.
    healthy = f"All-hands memo {uuid.uuid4().hex}".encode()
    (corpus / "memo.txt").write_bytes(healthy)

    free_sha_collisions(db, list(contents.values()))
    run_id = _run_pipeline_over(db, corpus)

    documents = {d.original_filename: d for d in _documents_for_run(db, run_id)}
    reasons = {}
    for d in documents.values():
        quarantine = db.scalar(select(Quarantine).where(Quarantine.document_id == d.id))
        reasons[d.original_filename] = quarantine.reason_code if quarantine else None

    # Reconciliation invariant: every document terminal, quarantined ones
    # each carry a reason row.
    assert all(d.status in ("done", "quarantined") for d in documents.values())

    assert reasons["zero_byte.txt"] == "zero_byte"
    assert reasons["password_protected.pdf"] == "password_protected"
    assert reasons["truncated.docx"] == "corrupt"  # surfaced at parse, not classify
    assert reasons["binary_junk.docx"] == "wrong_extension"
    assert reasons["noise.bin"] == "unsupported"
    assert reasons["memo.txt"] is None

    # The recoverable trap: xlsx-renamed-.pdf parses fine as xlsx.
    trap = documents["actually_xlsx.pdf"]
    assert reasons["actually_xlsx.pdf"] is None
    assert trap.status == "done"
    assert trap.file_class == "xlsx"
    assert "reclassified-from=pdf_digital" in trap.sniffed_mime
    assert len(trap.passages) > 0

    truncated = documents["truncated.docx"]
    quarantine = db.scalar(select(Quarantine).where(Quarantine.document_id == truncated.id))
    assert quarantine.stage == "parse"
    assert quarantine.detail  # exception detail preserved for the investigator agent

    run = ProcessingRunRepository(db).get(run_id)
    assert run.status == "finished"
    assert run.counters["quarantined"] == 5
    assert run.counters["done"] == 2  # memo.txt + the recovered trap
