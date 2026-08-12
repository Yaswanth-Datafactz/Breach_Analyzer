"""Data access for `accuracy_runs` / `accuracy_person_results` /
`accuracy_flag_results` (UC2's repositories layer). Write side used by
services/accuracy.py's scoring orchestration; read side serves
GET /accuracy/runs and GET /accuracy/runs/{id} -- API routers never call
select() directly (see repositories/runs.py's docstring), only this class.

`manifest_identities` / `manifest_elements` (the ground-truth import
target) deliberately have NO repository here: docs/plan.md's schema gives
them no manifest-origin column, so services/accuracy.py owns their access
directly (see that module's "MANIFEST SCOPING" note) rather than through a
new repositories/manifest.py file outside this task's assigned paths.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AccuracyFlagResult, AccuracyPersonResult, AccuracyRun


class AccuracyRunRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, *, config_snapshot: dict) -> AccuracyRun:
        run = AccuracyRun(
            config_snapshot=config_snapshot,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.flush()  # populate run.id without committing yet
        return run

    def get(self, accuracy_run_id: uuid.UUID) -> AccuracyRun | None:
        return self.db.get(AccuracyRun, accuracy_run_id)

    def get_with_results(self, accuracy_run_id: uuid.UUID) -> AccuracyRun | None:
        return self.db.scalar(
            select(AccuracyRun)
            .where(AccuracyRun.id == accuracy_run_id)
            .options(
                selectinload(AccuracyRun.person_results),
                selectinload(AccuracyRun.flag_results),
            )
        )

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[list[AccuracyRun], int]:
        total = self.db.scalar(select(func.count()).select_from(AccuracyRun)) or 0
        rows = self.db.scalars(
            select(AccuracyRun).order_by(AccuracyRun.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(rows), total

    def mark_finished(self, run: AccuracyRun, *, metrics: dict) -> None:
        run.metrics = metrics
        run.finished_at = datetime.now(timezone.utc)
        self.db.flush()

    def add_person_result(
        self,
        *,
        accuracy_run_id: uuid.UUID,
        manifest_person_uid: str,
        matched_person_id: uuid.UUID | None,
        match_basis: str | None,
        outcome: str,
    ) -> AccuracyPersonResult:
        row = AccuracyPersonResult(
            accuracy_run_id=accuracy_run_id,
            manifest_person_uid=manifest_person_uid,
            matched_person_id=matched_person_id,
            match_basis=match_basis,
            outcome=outcome,
        )
        self.db.add(row)
        return row

    def bulk_add_person_results(self, accuracy_run_id: uuid.UUID, rows: list) -> int:
        """`rows`: services.accuracy.PersonResultRow instances."""
        for row in rows:
            self.add_person_result(
                accuracy_run_id=accuracy_run_id,
                manifest_person_uid=row.manifest_person_uid,
                matched_person_id=row.matched_person_id,
                match_basis=row.match_basis,
                outcome=row.outcome,
            )
        self.db.flush()
        return len(rows)

    def add_flag_result(
        self,
        *,
        accuracy_run_id: uuid.UUID,
        manifest_person_uid: str,
        category: str,
        expected: bool,
        predicted: bool,
        outcome: str,
        error_class: str | None,
    ) -> AccuracyFlagResult:
        row = AccuracyFlagResult(
            accuracy_run_id=accuracy_run_id,
            manifest_person_uid=manifest_person_uid,
            category=category,
            expected=expected,
            predicted=predicted,
            outcome=outcome,
            error_class=error_class,
        )
        self.db.add(row)
        return row

    def bulk_add_flag_results(self, accuracy_run_id: uuid.UUID, rows: list) -> int:
        """`rows`: services.accuracy.FlagResultRow instances."""
        for row in rows:
            self.add_flag_result(
                accuracy_run_id=accuracy_run_id,
                manifest_person_uid=row.manifest_person_uid,
                category=row.category,
                expected=row.expected,
                predicted=row.predicted,
                outcome=row.outcome,
                error_class=row.error_class,
            )
        self.db.flush()
        return len(rows)

    def person_results_for(self, accuracy_run_id: uuid.UUID) -> list[AccuracyPersonResult]:
        return list(
            self.db.scalars(
                select(AccuracyPersonResult)
                .where(AccuracyPersonResult.accuracy_run_id == accuracy_run_id)
                .order_by(AccuracyPersonResult.manifest_person_uid.asc())
            )
        )

    def flag_results_for(self, accuracy_run_id: uuid.UUID) -> list[AccuracyFlagResult]:
        return list(
            self.db.scalars(
                select(AccuracyFlagResult)
                .where(AccuracyFlagResult.accuracy_run_id == accuracy_run_id)
                .order_by(AccuracyFlagResult.manifest_person_uid.asc(), AccuracyFlagResult.category.asc())
            )
        )
