"""Data access for `exposure_flags` + `flag_evidence` (UC2's repositories
layer). The two tables travel together on purpose: the §4 invariant -- no
flag without at least one evidence row -- is enforced by services/
exposure.py, and keeping both write paths in one repository means there is
exactly one place a flag could ever be written without its evidence."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import ExposureFlag, FlagEvidence, Person


class ExposureFlagRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, flag_id: uuid.UUID) -> ExposureFlag | None:
        return self.db.get(ExposureFlag, flag_id)

    def flags_for_person(self, person_id: uuid.UUID) -> list[ExposureFlag]:
        return list(
            self.db.scalars(
                select(ExposureFlag)
                .where(ExposureFlag.person_id == person_id)
                .order_by(ExposureFlag.category.asc())
            )
        )

    def flags_for_persons(self, person_ids: list[uuid.UUID]) -> list[ExposureFlag]:
        if not person_ids:
            return []
        return list(
            self.db.scalars(
                select(ExposureFlag)
                .where(ExposureFlag.person_id.in_(person_ids))
                .order_by(ExposureFlag.person_id.asc(), ExposureFlag.category.asc())
            )
        )

    def get_flag(self, person_id: uuid.UUID, category: str) -> ExposureFlag | None:
        return self.db.scalar(
            select(ExposureFlag).where(
                ExposureFlag.person_id == person_id, ExposureFlag.category == category
            )
        )

    def create_flag(
        self,
        *,
        person_id: uuid.UUID,
        category: str,
        exposed: bool,
        confidence: float | None,
        review_status: str = "auto",
    ) -> ExposureFlag:
        flag = ExposureFlag(
            person_id=person_id,
            category=category,
            exposed=exposed,
            confidence=confidence,
            review_status=review_status,
        )
        self.db.add(flag)
        self.db.flush()  # populate flag.id without committing yet
        return flag

    def add_evidence(
        self,
        *,
        exposure_flag_id: uuid.UUID,
        pii_element_id: uuid.UUID,
        document_id: uuid.UUID,
        passage_id: uuid.UUID,
        snippet: str | None,
    ) -> FlagEvidence:
        evidence = FlagEvidence(
            exposure_flag_id=exposure_flag_id,
            pii_element_id=pii_element_id,
            document_id=document_id,
            passage_id=passage_id,
            snippet=snippet,
        )
        self.db.add(evidence)
        self.db.flush()
        return evidence

    def evidence_for_flag(self, flag_id: uuid.UUID) -> list[FlagEvidence]:
        return list(
            self.db.scalars(
                select(FlagEvidence)
                .where(FlagEvidence.exposure_flag_id == flag_id)
                .order_by(FlagEvidence.created_at.asc(), FlagEvidence.id.asc())
            )
        )

    def clear_evidence(self, flag_id: uuid.UUID) -> int:
        result = self.db.execute(
            delete(FlagEvidence).where(FlagEvidence.exposure_flag_id == flag_id)
        )
        return result.rowcount or 0

    def delete_flag(self, flag: ExposureFlag) -> None:
        """Evidence rows cascade via the schema's ondelete rule."""
        self.db.delete(flag)
        self.db.flush()

    def evidence_counts_for_persons(
        self, person_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """flag_id -> evidence count would be per-flag; this is the person
        rollup the exposure listing shows. SQL GROUP BY, not Python."""
        if not person_ids:
            return {}
        rows = self.db.execute(
            select(ExposureFlag.person_id, func.count(FlagEvidence.id))
            .join(FlagEvidence, FlagEvidence.exposure_flag_id == ExposureFlag.id)
            .where(ExposureFlag.person_id.in_(person_ids))
            .group_by(ExposureFlag.person_id)
        ).all()
        return {row[0]: row[1] for row in rows}

    def flags_without_evidence(self, run_id: uuid.UUID) -> list[uuid.UUID]:
        """The §4 invariant checker: ids of flags with ZERO evidence rows.
        services/exposure.py raises if this is ever non-empty."""
        rows = self.db.execute(
            select(ExposureFlag.id)
            .join(Person, ExposureFlag.person_id == Person.id)
            .outerjoin(FlagEvidence, FlagEvidence.exposure_flag_id == ExposureFlag.id)
            .where(Person.run_id == run_id)
            .group_by(ExposureFlag.id)
            .having(func.count(FlagEvidence.id) == 0)
        ).all()
        return [row[0] for row in rows]
