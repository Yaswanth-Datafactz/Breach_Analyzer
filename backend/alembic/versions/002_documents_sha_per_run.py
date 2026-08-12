"""documents.sha256 unique per run, not globally

Revision ID: 002
Revises: 001
Create Date: 2026-08-12

docs/plan.md §14b R2: the global UNIQUE(sha256) deduped documents ACROSS
runs -- re-processing the same corpus in a new run produced zero rows, and
pytest fixture bytes that happened to equal corpusgen output (the zero-byte
problem file is the canonical case) collided with, and via test cleanup
deleted, production-run rows. UNIQUE(run_id, sha256) keeps within-run dedup
(the same attachment on five emails is still stored and parsed once per
run) while making DB rows per-run. Content-addressed BYTE storage
(services/parsing/storage.py, keyed by sha alone) still dedupes globally --
that layer is untouched.

Downgrade note: restoring the global constraint fails if any sha now exists
in more than one run; delete the extra runs' rows first (that cross-run
duplication is exactly what this migration legalized).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_documents_sha256", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_run_id_sha256", "documents", ["run_id", "sha256"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_documents_run_id_sha256", "documents", type_="unique")
    op.create_unique_constraint("uq_documents_sha256", "documents", ["sha256"])
