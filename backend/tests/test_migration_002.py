"""Migration 002 up/down (docs/plan.md §14b R2): documents.sha256 global
UNIQUE -> UNIQUE(run_id, sha256).

Runs against an EPHEMERAL database created on the same :5434 server -- the
dev database's data must never be touched by a downgrade test. Alembic runs
via subprocess with DATABASE_URL overridden (alembic/env.py reads settings,
and settings are env-driven), which is exactly how the migration runs in
every real environment."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
_TEST_DB = f"ba_migration_test_{uuid.uuid4().hex[:8]}"


def _server_url() -> str:
    # .../breach_analytics -> .../postgres for CREATE/DROP DATABASE
    base = get_settings().database_url
    return base.rsplit("/", 1)[0] + "/postgres"


def _test_db_url() -> str:
    return get_settings().database_url.rsplit("/", 1)[0] + f"/{_TEST_DB}"


def _alembic(direction: str, revision: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = _test_db_url()
    result = subprocess.run(
        [str(BACKEND_DIR / ".venv/bin/alembic"), direction, revision],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {direction} {revision} failed:\n{result.stderr}"


@pytest.fixture(scope="module")
def migration_db():
    admin = create_engine(_server_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{_TEST_DB}"'))
    engine = create_engine(_test_db_url())
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{_TEST_DB}' AND pid <> pg_backend_pid()"
                )
            )
            connection.execute(text(f'DROP DATABASE "{_TEST_DB}"'))
        admin.dispose()


def _constraint_names(engine) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'documents'::regclass AND contype = 'u'"
            )
        ).all()
    return {row[0] for row in rows}


def _insert_document(connection, run_id, sha: str) -> None:
    connection.execute(
        text(
            "INSERT INTO documents (id, run_id, sha256, original_filename, rel_path, "
            "byte_size, file_class, source_kind, status, created_at, updated_at) "
            "VALUES (:id, :run, :sha, 'f.txt', 'f.txt', 1, 'txt', 'corpus', 'done', "
            "now(), now())"
        ),
        {"id": str(uuid.uuid4()), "run": str(run_id), "sha": sha},
    )


def _insert_run(connection) -> uuid.UUID:
    run_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO processing_runs (id, config_snapshot, status, counters, "
            "created_at, updated_at) VALUES (:id, '{}', 'finished', '{}', now(), now())"
        ),
        {"id": str(run_id)},
    )
    return run_id


def test_upgrade_makes_sha_unique_per_run(migration_db):
    _alembic("upgrade", "head")
    names = _constraint_names(migration_db)
    assert "uq_documents_run_id_sha256" in names
    assert "uq_documents_sha256" not in names

    sha = "a" * 64
    with migration_db.begin() as connection:
        run_a = _insert_run(connection)
        run_b = _insert_run(connection)
        # Same sha in TWO runs: legal after 002.
        _insert_document(connection, run_a, sha)
        _insert_document(connection, run_b, sha)
    # Same sha TWICE in one run: still forbidden (within-run dedup intact).
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with migration_db.begin() as connection:
            _insert_document(connection, run_a, sha)


def test_downgrade_restores_global_unique_and_upgrade_returns(migration_db):
    # Downgrade requires no cross-run duplicates (documented in 002's
    # docstring) -- clear the rows the upgrade test legalized.
    with migration_db.begin() as connection:
        connection.execute(text("DELETE FROM documents"))
    _alembic("downgrade", "001")
    names = _constraint_names(migration_db)
    assert "uq_documents_sha256" in names
    assert "uq_documents_run_id_sha256" not in names

    from sqlalchemy.exc import IntegrityError

    sha = "b" * 64
    with pytest.raises(IntegrityError):
        with migration_db.begin() as connection:
            run_a = _insert_run(connection)
            run_b = _insert_run(connection)
            _insert_document(connection, run_a, sha)
            _insert_document(connection, run_b, sha)  # global unique again

    _alembic("upgrade", "head")
    assert "uq_documents_run_id_sha256" in _constraint_names(migration_db)
