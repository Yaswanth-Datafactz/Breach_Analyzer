"""Data access for `persons` (UC2's repositories layer) -- the exposure
table's spine. The exposure listing's search/filter/pagination is SQL here
(docs/plan.md §5: paginate/search/filter), never Python-side filtering: at
1M docs the persons table is the one the UI pages through."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Select, Text, delete, exists, func, select
from sqlalchemy.orm import Session

from app.db.models import ExposureFlag, Person, ProcessingRun


class PersonRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, person_id: uuid.UUID) -> Person | None:
        return self.db.get(Person, person_id)

    def create(
        self,
        *,
        run_id: uuid.UUID,
        best_name: str,
        aliases: list[dict] | None = None,
        dob: date | None = None,
        er_confidence: float | None = None,
        review_status: str = "auto",
        mention_count: int = 0,
        document_count: int = 0,
    ) -> Person:
        person = Person(
            run_id=run_id,
            best_name=best_name,
            aliases=aliases,
            dob=dob,
            er_confidence=er_confidence,
            review_status=review_status,
            mention_count=mention_count,
            document_count=document_count,
        )
        self.db.add(person)
        self.db.flush()  # populate person.id without committing yet
        return person

    def delete_for_run(self, run_id: uuid.UUID) -> int:
        """Wipe a run's ER output (persons cascade identity_links,
        exposure_flags, flag_evidence) ahead of an ER stage re-run."""
        result = self.db.execute(delete(Person).where(Person.run_id == run_id))
        return result.rowcount or 0

    def latest_run_with_persons(self) -> uuid.UUID | None:
        """Default run for /exposure when the caller names none: the most
        recently created run that actually has resolved persons."""
        return self.db.scalar(
            select(Person.run_id)
            .join(ProcessingRun, Person.run_id == ProcessingRun.id)
            .order_by(ProcessingRun.created_at.desc())
            .limit(1)
        )

    def _apply_exposure_filters(
        self,
        query: Select,
        *,
        run_id: uuid.UUID,
        search: str | None,
        category: str | None,
        review_status: str | None,
        min_confidence: float | None,
    ) -> Select:
        # mention_count > 0 hides persons absorbed by a reviewer merge
        # (their history stays in identity_links; they are no longer rows
        # in the exposure table).
        query = query.where(Person.run_id == run_id, Person.mention_count > 0)
        if search:
            # Name OR alias match, in SQL: aliases is JSONB
            # [{name, kind}, ...]; casting to text searches the alias names
            # without unnesting (kind strings never collide with name
            # patterns in practice, and a rare false hit only widens a page).
            pattern = f"%{search}%"
            query = query.where(
                Person.best_name.ilike(pattern)
                | func.cast(Person.aliases, Text).ilike(pattern)
            )
        if review_status:
            query = query.where(Person.review_status == review_status)
        flag_conditions = [ExposureFlag.person_id == Person.id, ExposureFlag.exposed.is_(True)]
        if category:
            flag_conditions.append(ExposureFlag.category == category)
        if min_confidence is not None:
            flag_conditions.append(ExposureFlag.confidence >= min_confidence)
        if category or min_confidence is not None:
            query = query.where(exists(select(ExposureFlag.id).where(*flag_conditions)))
        return query

    def list_exposure(
        self,
        *,
        run_id: uuid.UUID,
        search: str | None = None,
        category: str | None = None,
        review_status: str | None = None,
        min_confidence: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Person], int]:
        base = self._apply_exposure_filters(
            select(Person),
            run_id=run_id,
            search=search,
            category=category,
            review_status=review_status,
            min_confidence=min_confidence,
        )
        count_query = self._apply_exposure_filters(
            select(func.count()).select_from(Person),
            run_id=run_id,
            search=search,
            category=category,
            review_status=review_status,
            min_confidence=min_confidence,
        )
        total = self.db.scalar(count_query) or 0
        rows = self.db.scalars(
            base.order_by(Person.best_name.asc(), Person.id.asc()).limit(limit).offset(offset)
        ).all()
        return list(rows), total

    def iter_for_run(self, run_id: uuid.UUID) -> list[Person]:
        """Every visible person of a run, exposure-export order."""
        return list(
            self.db.scalars(
                select(Person)
                .where(Person.run_id == run_id, Person.mention_count > 0)
                .order_by(Person.best_name.asc(), Person.id.asc())
            )
        )
