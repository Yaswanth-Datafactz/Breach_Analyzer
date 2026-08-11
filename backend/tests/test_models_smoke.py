"""Schema smoke test against the REAL local Postgres (UC2's convention for
DB-touching tests -- see its tests/test_documents_api.py docstring): after
`alembic upgrade head`, every table models.py declares must exist, and the
two load-bearing partial unique indexes must have survived autogenerate
review. Catches models.py-vs-migration drift the moment it appears.

Requires the docker-compose postgres on :5434 with migrations applied.
"""

import uuid

from sqlalchemy import inspect

from app.db.base import Base
from app.db.models import Document, ProcessingRun  # noqa: F401 -- registers all models
from app.db.session import SessionLocal, engine


def test_every_model_table_exists_in_the_migrated_db():
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables.keys())
    missing = model_tables - db_tables
    assert not missing, f"models.py tables missing from the migrated DB: {sorted(missing)}"


def test_partial_unique_indexes_exist():
    inspector = inspect(engine)
    job_indexes = {ix["name"] for ix in inspector.get_indexes("extraction_jobs")}
    assert "uq_extraction_jobs_active_document" in job_indexes
    link_indexes = {ix["name"] for ix in inspector.get_indexes("identity_links")}
    assert "uq_identity_links_active_mention" in link_indexes


def test_run_and_document_round_trip_then_rollback():
    """One real INSERT through the FK chain proves defaults, server defaults,
    and the run->document FK actually work -- rolled back, never committed."""
    db = SessionLocal()
    try:
        run = ProcessingRun(config_snapshot={"smoke": True})
        db.add(run)
        db.flush()
        assert run.id is not None
        assert run.status == "created"
        assert run.created_at is not None  # server default fired

        document = Document(
            run_id=run.id,
            sha256=uuid.uuid4().hex * 2,  # 64 hex chars, unique per test run
            original_filename="smoke.txt",
            rel_path="smoke/smoke.txt",
            byte_size=0,
        )
        db.add(document)
        db.flush()
        assert document.id is not None
        assert document.status == "queued"
        assert document.file_class == "unknown"
    finally:
        db.rollback()
        db.close()
