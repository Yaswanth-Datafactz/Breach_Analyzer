"""Exposure computation (docs/plan.md §3's final pipeline step, §4's
invariant): per person x category flags derived from the linked mentions'
validated elements, every flag backed by evidence rows down to the exact
passage. This module is where "no flag without evidence" is ENFORCED --
`EvidenceInvariantViolation` is raised, not logged, if a flag would ever
exist without at least one evidence row.

Element admission rule (validators.py's status vocabulary, consumed
faithfully): for types a real verdict exists for (VERDICT_TYPES: ssn,
credit_card, phone, email) only 'valid' counts -- format_only there means
trap-downgraded (order numbers, staff signatures, placeholders) and
invalid_checksum means provably fake; both are exactly the §8 trap false
positives. For every OTHER category type (dob, drivers_license, passport,
financial_account, medical, credential, address, ssn_last4) no computable
check EXISTS, so format_only is the best attainable status and admits the
element -- provided no trap_reason signal downgraded it. Requiring 'valid'
across the board silently zeroed 7 of the 11 §1 flag categories (caught by
the B2/B3 integration pass: a fresh mini-corpus run produced only
ssn/email/phone/credit_card flags).

Confidence per flag = MAX composite confidence among the contributing
elements (documented choice): a category is exposed if its single best
piece of evidence is solid -- averaging would let ten weak OCR hits dilute
one checksum-valid SSN, and a min would let one noisy duplicate poison a
certain flag. Elements without a stored composite (tier-0 rows today) get
a deterministic detector-based default so the flag confidence is never
NULL while staying below 1.0: checksum-validated 0.95, regex 0.85,
reviewer 0.99, LLM tiers 0.75 until services/confidence.py stamps a real
composite.

Unattached elements (mention_id NULL -- the PartialIdentifiers cred dumps):
attached to a person via ER join keys -- an employee_id or SSN in the SAME
passage that exactly matches keys of exactly ONE person's linked evidence
-- and recorded on the element (signals.er_join_*) so a per-person
recompute reproduces the attachment. Elements matching zero or multiple
persons park as review_items kind=extraction, reason=unattached_pii: a
human (or the investigator agent) decides, nothing is silently dropped.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Document,
    IdentityLink,
    Mention,
    Passage,
    Person,
    PiiElement,
    ReviewItem,
)
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.persons import PersonRepository
from app.repositories.review import ReviewRepository
from app.services.detectors import run_tier0
from app.services.detectors import validators as detector_validators

logger = get_logger("exposure")

# element_type -> exposure category (docs/plan.md §1's category list).
# ssn_last4 maps to ssn: a partial SSN is SSN-category exposure with its own
# evidence trail (the snippet shows it is a last-4) -- flagged for the
# accuracy owner in case the manifest scores partials differently. `name`
# maps to nothing: names anchor identity, they are not an exposure category.
CATEGORY_BY_ELEMENT_TYPE: dict[str, str] = {
    "ssn": "ssn",
    "ssn_last4": "ssn",
    "dob": "dob",
    "drivers_license": "drivers_license",
    "passport": "passport",
    "financial_account": "financial_account",
    "credit_card": "credit_card",
    "medical": "medical",
    "credential": "credentials",
    "address": "address",
    "phone": "phone",
    "email": "email",
}

_DEFAULT_CONFIDENCE_BY_DETECTOR = {
    "checksum": 0.95,
    "regex": 0.85,
    "llm_tier1": 0.75,
    "llm_tier2": 0.75,
    "reviewer": 0.99,
}

UNATTACHED_REASON = "unattached_pii"

# ER join key types for unattached-element attachment (task spec:
# employee_id / ssn exact match to a person's elements).
_JOIN_KEY_ELEMENT_TYPES = ("ssn",)


class EvidenceInvariantViolation(RuntimeError):
    """A flag would exist without evidence -- the §4 invariant. Raised, not
    logged: an unevidenced flag is indefensible output."""


@dataclass
class ExposureResult:
    run_id: uuid.UUID
    persons: int = 0
    flags: int = 0
    evidence: int = 0
    attached_via_join: int = 0
    parked_unattached: int = 0
    auto_dismissed: int = 0
    skipped_not_valid: int = 0
    categories: dict[str, int] = field(default_factory=dict)


@dataclass
class _EvidenceElement:
    element: PiiElement
    category: str
    confidence: float


def _element_confidence(element: PiiElement) -> float:
    if element.confidence is not None:
        return float(element.confidence)
    return _DEFAULT_CONFIDENCE_BY_DETECTOR.get(element.detector, 0.5)


def _usable(element: PiiElement) -> bool:
    """The module-docstring admission rule: verdict-capable types must be
    'valid'; unverifiable types admit plain format_only, but never a
    trap-downgraded row (signals.trap_reason)."""
    if element.element_type not in CATEGORY_BY_ELEMENT_TYPE:
        return False
    if (element.signals or {}).get("trap_reason"):
        return False
    if element.element_type in detector_validators.VERDICT_TYPES:
        return element.validation_status == "valid"
    return element.validation_status in ("valid", "format_only")


# ---------------------------------------------------------------------------
# Person evidence collection
# ---------------------------------------------------------------------------


def _linked_elements_by_person(
    db: Session, person_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PiiElement]]:
    """Elements attached to a person's actively linked mentions."""
    if not person_ids:
        return {}
    rows = db.execute(
        select(IdentityLink.person_id, PiiElement)
        .join(Mention, IdentityLink.mention_id == Mention.id)
        .join(PiiElement, PiiElement.mention_id == Mention.id)
        .where(IdentityLink.person_id.in_(person_ids), IdentityLink.active.is_(True))
        .order_by(PiiElement.created_at.asc(), PiiElement.id.asc())
    ).all()
    by_person: dict[uuid.UUID, list[PiiElement]] = defaultdict(list)
    for person_id, element in rows:
        by_person[person_id].append(element)
    return by_person


def _signal_attached_elements_by_person(
    db: Session, run_id: uuid.UUID
) -> dict[uuid.UUID, list[PiiElement]]:
    """Unlinked elements previously attached by a reviewer decision or an
    earlier join-key pass (signals.reviewer_attached_person_id /
    signals.er_join_person_id) -- re-read so recomputes are stable."""
    rows = db.scalars(
        select(PiiElement)
        .join(Document, PiiElement.document_id == Document.id)
        .where(
            Document.run_id == run_id,
            PiiElement.mention_id.is_(None),
            PiiElement.signals.isnot(None),
        )
        .order_by(PiiElement.created_at.asc(), PiiElement.id.asc())
    ).all()
    by_person: dict[uuid.UUID, list[PiiElement]] = defaultdict(list)
    for element in rows:
        signals = element.signals or {}
        target = signals.get("reviewer_attached_person_id") or signals.get(
            "er_join_person_id"
        )
        if target:
            by_person[uuid.UUID(target)].append(element)
    return by_person


# ---------------------------------------------------------------------------
# ER join keys for unattached elements
# ---------------------------------------------------------------------------


def _person_key_index(
    db: Session, run_id: uuid.UUID, linked_by_person: dict[uuid.UUID, list[PiiElement]]
) -> dict[tuple[str, str], set[uuid.UUID]]:
    """(key_type, normalized_value) -> person ids holding that key.
    ssn keys come from the persons' linked valid elements; employee_id keys
    from mentions.features (persisted there by er/persist.py's tier-0
    re-run)."""
    index: dict[tuple[str, str], set[uuid.UUID]] = defaultdict(set)
    for person_id, elements in linked_by_person.items():
        for element in elements:
            if element.element_type in _JOIN_KEY_ELEMENT_TYPES and _usable(element):
                index[(element.element_type, element.value_normalized)].add(person_id)
    rows = db.execute(
        select(IdentityLink.person_id, Mention.features)
        .join(Mention, IdentityLink.mention_id == Mention.id)
        .join(Person, IdentityLink.person_id == Person.id)
        .where(Person.run_id == run_id, IdentityLink.active.is_(True))
    ).all()
    for person_id, features in rows:
        for key in (features or {}).get("employee_id") or []:
            index[("employee_id", key)].add(person_id)
    return index


def _passage_join_keys(db: Session, passage_ids: list[uuid.UUID]) -> dict[uuid.UUID, set[tuple[str, str]]]:
    """Join keys present in each passage: employee_id hits from a tier-0
    re-run (they are never pii_elements rows -- §14b B2) plus valid SSN
    elements already stored for that passage."""
    keys: dict[uuid.UUID, set[tuple[str, str]]] = defaultdict(set)
    if not passage_ids:
        return keys
    rows = db.execute(
        select(Passage.id, Passage.text).where(Passage.id.in_(passage_ids))
    ).all()
    for passage_id, text in rows:
        for hit in run_tier0(text):
            if hit.element_type == "employee_id" and hit.trap_reason is None:
                keys[passage_id].add(("employee_id", hit.value_normalized))
    ssn_rows = db.execute(
        select(PiiElement.passage_id, PiiElement.value_normalized).where(
            PiiElement.passage_id.in_(passage_ids),
            PiiElement.element_type == "ssn",
            PiiElement.validation_status == "valid",
        )
    ).all()
    for passage_id, value in ssn_rows:
        keys[passage_id].add(("ssn", value))
    return keys


def _attach_or_park_unattached(
    db: Session,
    run_id: uuid.UUID,
    result: ExposureResult,
    linked_by_person: dict[uuid.UUID, list[PiiElement]],
    extra_by_person: dict[uuid.UUID, list[PiiElement]],
) -> None:
    """Resolve every run-level unattached element: join to exactly one
    person (recorded on the element's signals) or park for review."""
    unattached = list(
        db.scalars(
            select(PiiElement)
            .join(Document, PiiElement.document_id == Document.id)
            .where(Document.run_id == run_id, PiiElement.mention_id.is_(None))
            .order_by(PiiElement.created_at.asc(), PiiElement.id.asc())
        )
    )
    if not unattached:
        return

    key_index = _person_key_index(db, run_id, linked_by_person)
    passage_keys = _passage_join_keys(db, sorted({e.passage_id for e in unattached}))
    review_repo = ReviewRepository(db)

    for element in unattached:
        signals = dict(element.signals or {})
        if signals.get("reviewer_attached_person_id") or signals.get("er_join_person_id"):
            continue  # already resolved on a previous pass
        if not _usable(element):
            result.skipped_not_valid += 1
            continue

        element_keys = set(passage_keys.get(element.passage_id, ()))
        if element.element_type in _JOIN_KEY_ELEMENT_TYPES:
            element_keys.add((element.element_type, element.value_normalized))
        candidates: set[uuid.UUID] = set()
        matched_key: tuple[str, str] | None = None
        for key in sorted(element_keys):
            persons = key_index.get(key, set())
            if persons:
                candidates |= persons
                matched_key = matched_key or key

        if len(candidates) == 1 and matched_key is not None:
            person_id = next(iter(candidates))
            signals["er_join_person_id"] = str(person_id)
            signals["er_join_key"] = f"{matched_key[0]}:{matched_key[1]}"
            element.signals = signals  # fresh dict: JSONB change tracking
            extra_by_person[person_id].append(element)
            result.attached_via_join += 1
        else:
            if review_repo.find_item_for_element(element.id) is None:
                review_repo.create_item(
                    kind="extraction",
                    ref={"pii_element_id": str(element.id), "run_id": str(run_id)},
                    reason=UNATTACHED_REASON,
                    priority=0,
                )
                result.parked_unattached += 1

    _dismiss_obsolete_unattached_items(db, run_id, result)
    db.flush()


def _dismiss_obsolete_unattached_items(
    db: Session, run_id: uuid.UUID, result: ExposureResult
) -> None:
    """Close open unattached_pii items whose element has since been
    resolved -- attached by this pass's join keys, by a reviewer, or by a
    deeper extraction run giving it a mention (tier-1 attaching a tier-0
    hit). Parked-then-resolved must not linger as queue noise; the closure
    is recorded as a system decision, never a silent delete."""
    open_items = list(
        db.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == "extraction",
                ReviewItem.status == "open",
                ReviewItem.reason == UNATTACHED_REASON,
                ReviewItem.ref["run_id"].astext == str(run_id),
            )
        )
    )
    review_repo = ReviewRepository(db)
    for item in open_items:
        element_id = (item.ref or {}).get("pii_element_id")
        element = db.get(PiiElement, uuid.UUID(element_id)) if element_id else None
        if element is None:
            continue
        signals = element.signals or {}
        resolved = (
            element.mention_id is not None
            or signals.get("er_join_person_id")
            or signals.get("reviewer_attached_person_id")
        )
        if resolved:
            item.status = "dismissed"
            review_repo.create_decision(
                review_item_id=item.id,
                decision="auto_attached",
                reviewer="system",
                notes="element attached after parking (mention or ER join key); "
                "queue item obsolete",
            )
            result.auto_dismissed += 1


# ---------------------------------------------------------------------------
# Flag writing (the invariant lives here)
# ---------------------------------------------------------------------------


def _snippet_for(element: PiiElement) -> str | None:
    return element.context_snippet


def _write_person_flags(
    db: Session, person: Person, elements: list[PiiElement], result: ExposureResult
) -> None:
    repo = ExposureFlagRepository(db)
    by_category: dict[str, list[_EvidenceElement]] = defaultdict(list)
    seen_element_ids: set[uuid.UUID] = set()
    for element in elements:
        if element.id in seen_element_ids:
            continue  # same element reachable via two mentions of one person
        seen_element_ids.add(element.id)
        if not _usable(element):
            result.skipped_not_valid += 1
            continue
        category = CATEGORY_BY_ELEMENT_TYPE[element.element_type]
        by_category[category].append(
            _EvidenceElement(element, category, _element_confidence(element))
        )

    existing = {flag.category: flag for flag in repo.flags_for_person(person.id)}

    for category, evidence_elements in sorted(by_category.items()):
        if not evidence_elements:
            # Structurally unreachable (a category key only exists once an
            # element joined it) -- kept as a real guard because this is THE
            # invariant, not a style preference.
            raise EvidenceInvariantViolation(
                f"flag {person.id}/{category} would be written with zero evidence"
            )
        flag = existing.pop(category, None)
        confidence = max(e.confidence for e in evidence_elements)
        if flag is None:
            flag = repo.create_flag(
                person_id=person.id, category=category, exposed=True, confidence=confidence
            )
        elif flag.review_status == "auto":
            flag.exposed = True
            flag.confidence = confidence
        else:
            # A human confirmed/overrode this flag -- recompute refreshes
            # the evidence set below but never the human's verdict.
            pass
        repo.clear_evidence(flag.id)
        for item in evidence_elements:
            repo.add_evidence(
                exposure_flag_id=flag.id,
                pii_element_id=item.element.id,
                document_id=item.element.document_id,
                passage_id=item.element.passage_id,
                snippet=_snippet_for(item.element),
            )
            result.evidence += 1
        result.flags += 1
        result.categories[category] = result.categories.get(category, 0) + 1

    # Categories no longer evidenced: auto flags go (with their evidence,
    # via cascade); human-decided flags stay -- but a human flag left with
    # zero evidence would break the invariant, so its evidence is preserved
    # by simply not clearing it (we only cleared flags we rebuilt above).
    for category, flag in existing.items():
        if flag.review_status == "auto":
            repo.delete_flag(flag)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def compute_exposure(
    db: Session,
    run_id: uuid.UUID,
    *,
    person_ids: list[uuid.UUID] | None = None,
) -> ExposureResult:
    """Compute (or recompute) exposure flags + evidence.

    Full-run mode (person_ids=None) also resolves unattached elements --
    join-key attach or review parking. Person-scoped mode (the review
    decision projections) rebuilds just those persons, re-reading any
    signal-recorded attachments so corrections converge. Commits nothing:
    the caller owns the transaction. Ends by asserting the §4 invariant
    over the whole run and raising EvidenceInvariantViolation if any flag
    lacks evidence."""
    result = ExposureResult(run_id=run_id)
    persons_repo = PersonRepository(db)

    if person_ids is None:
        persons = list(
            db.scalars(select(Person).where(Person.run_id == run_id).order_by(Person.created_at))
        )
    else:
        persons = [p for p in (persons_repo.get(pid) for pid in person_ids) if p is not None]

    scope_ids = [p.id for p in persons]
    linked_by_person = _linked_elements_by_person(db, scope_ids)
    extra_by_person = _signal_attached_elements_by_person(db, run_id)

    if person_ids is None:
        _attach_or_park_unattached(db, run_id, result, linked_by_person, extra_by_person)

    for person in persons:
        elements = linked_by_person.get(person.id, []) + extra_by_person.get(person.id, [])
        _write_person_flags(db, person, elements, result)
        result.persons += 1

    db.flush()
    # Final structural assertion of the §4 invariant across the run --
    # catches any writer that bypassed _write_person_flags.
    orphaned = ExposureFlagRepository(db).flags_without_evidence(run_id)
    if orphaned:
        raise EvidenceInvariantViolation(
            f"{len(orphaned)} exposure flag(s) have no evidence rows: "
            f"{[str(f) for f in orphaned[:5]]}"
        )

    logger.info(
        "exposure_computed",
        run_id=str(run_id),
        persons=result.persons,
        flags=result.flags,
        evidence=result.evidence,
        attached_via_join=result.attached_via_join,
        parked_unattached=result.parked_unattached,
        scope="run" if person_ids is None else "persons",
    )
    return result


def recompute_for_persons(db: Session, run_id: uuid.UUID, person_ids: list[uuid.UUID]) -> ExposureResult:
    """Review-decision projection entry point (docs/plan.md §4: decisions
    project back)."""
    return compute_exposure(db, run_id, person_ids=person_ids)
