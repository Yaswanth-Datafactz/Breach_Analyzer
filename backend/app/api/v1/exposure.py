"""GET /exposure (paginate/search/filter) and GET /persons/{id} -- the
exposure table itself (docs/plan.md §5's API surface, §7's Exposure and
Person-detail pages).

The router carries no prefix (same reasoning as documents.py): /exposure
and /persons/{id} are one resource group -- a person row is only ever
reached FROM the exposure table. All filtering happens in SQL
(repositories/persons.py), never in Python: this is the table that grows
to millions of rows at 100x.

Run scoping: every endpoint takes an optional ?run_id=; omitted, it
resolves to the most recent run that has resolved persons -- the demo and
the deployed app both serve exactly one populated run, so the default is
what the UI wants, while accuracy tooling can pin a run explicitly."""

from __future__ import annotations

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.core.security import require_api_key
from app.db.models import Document, FlagEvidence, Mention, PiiElement
from app.db.session import get_db
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.persons import PersonRepository
from app.schemas.errors import ErrorEnvelope
from app.schemas.exposure import (
    AliasOut,
    EvidenceOut,
    ExposurePageOut,
    FlagDetailOut,
    FlagOut,
    IdentityLinkOut,
    PersonDetailOut,
    PersonSummaryOut,
)

router = APIRouter(
    tags=["exposure"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)


def _resolve_run_id(db: Session, run_id: uuid.UUID | None) -> uuid.UUID:
    if run_id is not None:
        return run_id
    resolved = PersonRepository(db).latest_run_with_persons()
    if resolved is None:
        raise NotFoundError("No run has resolved persons yet -- run the ER stage first")
    return resolved


def _aliases_out(aliases: list | None) -> list[AliasOut]:
    return [AliasOut(name=a["name"], kind=a.get("kind", "misspelling")) for a in aliases or []]


@router.get("/exposure", response_model=ExposurePageOut)
def list_exposure(
    run_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, description="name or alias substring"),
    category: str | None = Query(default=None, description="exposure category filter"),
    review_status: str | None = Query(default=None),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ExposurePageOut:
    resolved_run = _resolve_run_id(db, run_id)
    persons, total = PersonRepository(db).list_exposure(
        run_id=resolved_run,
        search=search,
        category=category,
        review_status=review_status,
        min_confidence=min_confidence,
        limit=limit,
        offset=offset,
    )
    flags_repo = ExposureFlagRepository(db)
    person_ids = [p.id for p in persons]
    flags = flags_repo.flags_for_persons(person_ids)
    evidence_counts: dict[uuid.UUID, int] = defaultdict(int)
    if flags:
        rows = db.execute(
            select(FlagEvidence.exposure_flag_id)
            .where(FlagEvidence.exposure_flag_id.in_([f.id for f in flags]))
        ).all()
        for (flag_id,) in rows:
            evidence_counts[flag_id] += 1
    flags_by_person: dict[uuid.UUID, list[FlagOut]] = defaultdict(list)
    for flag in flags:
        flags_by_person[flag.person_id].append(
            FlagOut(
                id=flag.id,
                category=flag.category,
                exposed=flag.exposed,
                confidence=float(flag.confidence) if flag.confidence is not None else None,
                review_status=flag.review_status,
                evidence_count=evidence_counts.get(flag.id, 0),
            )
        )

    items = [
        PersonSummaryOut(
            id=p.id,
            run_id=p.run_id,
            best_name=p.best_name,
            aliases=_aliases_out(p.aliases),
            dob=p.dob,
            er_confidence=float(p.er_confidence) if p.er_confidence is not None else None,
            review_status=p.review_status,
            mention_count=p.mention_count,
            document_count=p.document_count,
            flags=flags_by_person.get(p.id, []),
        )
        for p in persons
    ]
    return ExposurePageOut(
        items=items, total=total, limit=limit, offset=offset, run_id=resolved_run
    )


@router.get(
    "/persons/{person_id}",
    response_model=PersonDetailOut,
    tags=["persons"],
    responses={404: {"description": "Person not found", "model": ErrorEnvelope}},
)
def get_person(person_id: uuid.UUID, db: Session = Depends(get_db)) -> PersonDetailOut:
    person = PersonRepository(db).get(person_id)
    if person is None:
        raise NotFoundError(f"Person {person_id} not found")

    flags_repo = ExposureFlagRepository(db)
    flags_out: list[FlagDetailOut] = []
    for flag in flags_repo.flags_for_person(person.id):
        evidence_rows = db.execute(
            select(FlagEvidence, PiiElement, Document)
            .join(PiiElement, FlagEvidence.pii_element_id == PiiElement.id)
            .join(Document, FlagEvidence.document_id == Document.id)
            .where(FlagEvidence.exposure_flag_id == flag.id)
            .order_by(FlagEvidence.created_at.asc(), FlagEvidence.id.asc())
        ).all()
        evidence = [
            EvidenceOut(
                id=ev.id,
                pii_element_id=element.id,
                element_type=element.element_type,
                document_id=document.id,
                document_filename=document.original_filename,
                passage_id=ev.passage_id,
                snippet=ev.snippet,
                char_start=element.char_start,
                char_end=element.char_end,
                detector=element.detector,
                validation_status=element.validation_status,
            )
            for ev, element, document in evidence_rows
        ]
        flags_out.append(
            FlagDetailOut(
                id=flag.id,
                category=flag.category,
                exposed=flag.exposed,
                confidence=float(flag.confidence) if flag.confidence is not None else None,
                review_status=flag.review_status,
                evidence_count=len(evidence),
                evidence=evidence,
            )
        )

    links_out: list[IdentityLinkOut] = []
    for link in IdentityLinkRepository(db).all_links_for_person(person.id):
        row = db.execute(
            select(Mention.name_raw, Document.original_filename)
            .join(Document, Mention.document_id == Document.id)
            .where(Mention.id == link.mention_id)
        ).first()
        links_out.append(
            IdentityLinkOut(
                id=link.id,
                mention_id=link.mention_id,
                mention_name_raw=row[0] if row else None,
                document_filename=row[1] if row else None,
                score=float(link.score) if link.score is not None else None,
                method=link.method,
                rule_id=link.rule_id,
                rationale=link.rationale,
                active=link.active,
                created_at=link.created_at,
            )
        )

    return PersonDetailOut(
        id=person.id,
        run_id=person.run_id,
        best_name=person.best_name,
        aliases=_aliases_out(person.aliases),
        dob=person.dob,
        er_confidence=float(person.er_confidence) if person.er_confidence is not None else None,
        review_status=person.review_status,
        mention_count=person.mention_count,
        document_count=person.document_count,
        flags=flags_out,
        identity_links=links_out,
    )
