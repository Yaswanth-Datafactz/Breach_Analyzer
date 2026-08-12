"""Review queue service (docs/plan.md §4: review_items/review_decisions;
§5: GET /review/items, POST /review/items/{id}/decision). Two halves:

- LISTING with resolved refs: a review item's ref is {ids}; the reviewer
  UI needs the evidence behind them. extraction items resolve to the
  element + its passage anchor; er_pair items resolve BOTH mentions'
  evidence sets side by side (name, document, passage, elements) plus the
  pair score -- the plan's side-by-side review page renders this directly.

- DECISIONS that project back (§4: "Decisions project back: element
  correction -> recompute flags; ER decision -> identity_links"):
  * extraction: correct (patch element fields) / attach (bind an
    unattached element to a person) / dismiss (downgrade to format_only)
    -- each followed by an exposure recompute for the affected person(s).
  * er_pair: merge (move the absorbed person's mentions to the survivor --
    append-only: old links flip inactive, new reviewer links are inserted)
    / keep_separate (records the no-merge; if the mentions had since been
    merged into one person, SPLITS the right mention back out into a new
    person, again via flip + insert). Both write an er_decisions row with
    method='reviewer' and recompute exposure for every affected person.

Race safety (UC2 pattern): the caller-facing decide() locks the item row
FOR UPDATE, re-checks status == open, and applies everything in the one
transaction -- a concurrent second decision blocks on the lock and then
sees 'decided', mapping to 409, never a double-apply. The identity_links
partial unique index backstops the merge path at the DB level.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.db.models import Document, Mention, Person, PiiElement, ReviewItem
from app.repositories.er_decisions import ErDecisionRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.persons import PersonRepository
from app.repositories.review import ReviewRepository
from app.services.exposure import CATEGORY_BY_ELEMENT_TYPE, recompute_for_persons

logger = get_logger("review")

_EXTRACTION_DECISIONS = frozenset({"correct", "attach", "dismiss"})
_ER_DECISIONS = frozenset({"merge", "keep_separate"})
_FLAG_DECISIONS = frozenset({"confirm", "override"})

_CORRECTABLE_FIELDS = frozenset({"element_type", "validation_status"})


# ---------------------------------------------------------------------------
# Ref resolution (listing)
# ---------------------------------------------------------------------------


def _element_card(db: Session, element: PiiElement) -> dict:
    document = db.get(Document, element.document_id)
    return {
        "pii_element_id": str(element.id),
        "element_type": element.element_type,
        "value_raw": element.value_raw,
        "validation_status": element.validation_status,
        "detector": element.detector,
        "confidence": float(element.confidence) if element.confidence is not None else None,
        "document_id": str(element.document_id),
        "document_filename": document.original_filename if document else None,
        "passage_id": str(element.passage_id),
        "char_start": element.char_start,
        "char_end": element.char_end,
        "snippet": element.context_snippet,
        "signals": element.signals,
    }


def _mention_card(db: Session, mention_id: uuid.UUID) -> dict | None:
    mention = db.get(Mention, mention_id)
    if mention is None:
        return None
    document = db.get(Document, mention.document_id)
    elements = list(
        db.scalars(
            select(PiiElement)
            .where(PiiElement.mention_id == mention.id)
            .order_by(PiiElement.element_type.asc(), PiiElement.id.asc())
        )
    )
    link = IdentityLinkRepository(db).active_link_for_mention(mention.id)
    return {
        "mention_id": str(mention.id),
        "name_raw": mention.name_raw,
        "dob": mention.dob.isoformat() if mention.dob else None,
        "document_id": str(mention.document_id),
        "document_filename": document.original_filename if document else None,
        "passage_id": str(mention.passage_id),
        "person_id": str(link.person_id) if link else None,
        "features": mention.features,
        "elements": [
            {
                "pii_element_id": str(e.id),
                "element_type": e.element_type,
                "value_raw": e.value_raw,
                "validation_status": e.validation_status,
                "char_start": e.char_start,
                "char_end": e.char_end,
            }
            for e in elements
        ],
    }


def resolve_item_ref(db: Session, item: ReviewItem) -> dict:
    """The kind-shaped 'resolved' payload the review UI renders."""
    ref = item.ref or {}
    if item.kind == "extraction":
        element_id = ref.get("pii_element_id")
        element = db.get(PiiElement, uuid.UUID(element_id)) if element_id else None
        return {"element": _element_card(db, element) if element else None}
    if item.kind == "er_pair":
        left = ref.get("left_mention_id")
        right = ref.get("right_mention_id")
        return {
            "score": ref.get("score"),
            "left": _mention_card(db, uuid.UUID(left)) if left else None,
            "right": _mention_card(db, uuid.UUID(right)) if right else None,
        }
    return {"ref": ref}  # flag_audit and future kinds: pass-through


def list_items_with_refs(
    db: Session,
    *,
    kind: str | None,
    status: str | None,
    run_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[ReviewItem, dict]], int]:
    items, total = ReviewRepository(db).list_items(
        kind=kind, status=status, run_id=run_id, limit=limit, offset=offset
    )
    return [(item, resolve_item_ref(db, item)) for item in items], total


# ---------------------------------------------------------------------------
# Decision application
# ---------------------------------------------------------------------------


@dataclass
class DecisionOutcome:
    item_id: uuid.UUID
    decision: str
    recomputed_person_ids: list[uuid.UUID]
    detail: dict


def _run_id_of(item: ReviewItem) -> uuid.UUID:
    run_id = (item.ref or {}).get("run_id")
    if not run_id:
        raise ValidationFailedError(
            f"review item {item.id} carries no run_id in its ref; cannot project"
        )
    return uuid.UUID(run_id)


def _person_of_element(db: Session, element: PiiElement) -> uuid.UUID | None:
    if element.mention_id is not None:
        link = IdentityLinkRepository(db).active_link_for_mention(element.mention_id)
        if link:
            return link.person_id
    signals = element.signals or {}
    target = signals.get("reviewer_attached_person_id") or signals.get("er_join_person_id")
    return uuid.UUID(target) if target else None


def _apply_extraction_decision(
    db: Session, item: ReviewItem, decision: str, after: dict, reviewer: str, notes: str | None
) -> DecisionOutcome:
    ref = item.ref or {}
    element = db.get(PiiElement, uuid.UUID(ref["pii_element_id"]))
    if element is None:
        raise NotFoundError(f"pii_element {ref.get('pii_element_id')} no longer exists")
    run_id = _run_id_of(item)
    before = {
        "element_type": element.element_type,
        "validation_status": element.validation_status,
        "signals": element.signals,
    }
    affected: set[uuid.UUID] = set()
    prior_person = _person_of_element(db, element)
    if prior_person:
        affected.add(prior_person)

    if decision == "correct":
        unknown = set(after) - _CORRECTABLE_FIELDS
        if unknown or not after:
            raise ValidationFailedError(
                "correct requires an 'after' payload with element_type and/or "
                f"validation_status (got {sorted(after) or 'nothing'})"
            )
        if "element_type" in after:
            if after["element_type"] not in CATEGORY_BY_ELEMENT_TYPE and after["element_type"] != "name":
                raise ValidationFailedError(f"unknown element_type {after['element_type']!r}")
            element.element_type = after["element_type"]
        if "validation_status" in after:
            if after["validation_status"] not in ("valid", "invalid_checksum", "format_only"):
                raise ValidationFailedError(
                    f"unknown validation_status {after['validation_status']!r}"
                )
            element.validation_status = after["validation_status"]
        element.detector = "reviewer"
    elif decision == "attach":
        person_id = after.get("person_id")
        if not person_id:
            raise ValidationFailedError("attach requires after.person_id")
        person = PersonRepository(db).get(uuid.UUID(person_id))
        if person is None:
            raise NotFoundError(f"person {person_id} not found")
        signals = dict(element.signals or {})
        signals["reviewer_attached_person_id"] = str(person.id)
        signals.pop("er_join_person_id", None)
        element.signals = signals
        affected.add(person.id)
    elif decision == "dismiss":
        element.validation_status = "format_only"
        signals = dict(element.signals or {})
        signals["trap_reason"] = "reviewer_dismissed"
        element.signals = signals
    db.flush()

    recomputed = sorted(affected)
    if recomputed:
        recompute_for_persons(db, run_id, recomputed)
    return DecisionOutcome(
        item_id=item.id,
        decision=decision,
        recomputed_person_ids=recomputed,
        detail={"before": before},
    )


def _refresh_person_counters(db: Session, person: Person) -> None:
    links = IdentityLinkRepository(db).active_links_for_person(person.id)
    mention_ids = [link.mention_id for link in links]
    if mention_ids:
        docs = db.scalars(
            select(Mention.document_id).where(Mention.id.in_(mention_ids)).distinct()
        ).all()
        person.mention_count = len(mention_ids)
        person.document_count = len(docs)
    else:
        person.mention_count = 0
        person.document_count = 0
    db.flush()


def _merge_persons(
    db: Session,
    survivor: Person,
    absorbed: Person,
    *,
    score: float | None,
    reviewer: str,
    notes: str | None,
) -> int:
    """Move every active link of `absorbed` onto `survivor`, append-only:
    flip old rows inactive, insert reviewer rows. Returns links moved."""
    links_repo = IdentityLinkRepository(db)
    moved = 0
    for link in links_repo.active_links_for_person(absorbed.id):
        links_repo.deactivate(link)
        links_repo.create(
            person_id=survivor.id,
            mention_id=link.mention_id,
            method="reviewer",
            score=score,
            rule_id="reviewer_merge",
            rationale=notes or f"reviewer {reviewer} merged persons",
        )
        moved += 1
    # Signal-attached unlinked elements (exposure's ER-join / reviewer
    # attachments) follow the identity: left pointing at the absorbed
    # person, they would rebuild flags on an empty person at the next
    # recompute.
    attached = db.scalars(
        select(PiiElement).where(
            PiiElement.signals.isnot(None),
            (PiiElement.signals["er_join_person_id"].astext == str(absorbed.id))
            | (PiiElement.signals["reviewer_attached_person_id"].astext == str(absorbed.id)),
        )
    ).all()
    for element in attached:
        signals = dict(element.signals)
        for key in ("er_join_person_id", "reviewer_attached_person_id"):
            if signals.get(key) == str(absorbed.id):
                signals[key] = str(survivor.id)
        element.signals = signals  # fresh dict: JSONB change tracking
    # The absorbed identity's names travel as aliases of the survivor,
    # kind-classified against the surviving best name (same vocabulary the
    # ER stage uses).
    from app.services.er.normalize import normalize_name
    from app.services.er.persist import classify_alias_kind

    best_norm = normalize_name(survivor.best_name)
    aliases = list(survivor.aliases or [])
    known = {a["name"] for a in aliases} | {survivor.best_name}
    for candidate in [absorbed.best_name, *[a["name"] for a in (absorbed.aliases or [])]]:
        if candidate not in known:
            aliases.append(
                {
                    "name": candidate,
                    "kind": classify_alias_kind(normalize_name(candidate), best_norm),
                }
            )
            known.add(candidate)
    survivor.aliases = aliases or None
    if survivor.dob is None:
        survivor.dob = absorbed.dob
    merged_confidences = [
        c
        for c in (survivor.er_confidence, absorbed.er_confidence, score)
        if c is not None
    ]
    survivor.er_confidence = min(merged_confidences) if merged_confidences else None
    survivor.review_status = "human_confirmed"
    absorbed.review_status = "human_confirmed"
    _refresh_person_counters(db, survivor)
    _refresh_person_counters(db, absorbed)
    return moved


def _split_mention_to_new_person(
    db: Session, mention: Mention, run_id: uuid.UUID, reviewer: str, notes: str | None
) -> Person:
    """Undo a merge for one mention: flip its active link inactive (the
    plan's 'unlink = new inactive row' ledger discipline) and give the
    mention a fresh person under a reviewer link."""
    links_repo = IdentityLinkRepository(db)
    link = links_repo.active_link_for_mention(mention.id)
    person = PersonRepository(db).create(
        run_id=run_id,
        best_name=mention.name_raw,
        aliases=None,
        dob=mention.dob,
        er_confidence=1.0,
        review_status="human_confirmed",
        mention_count=1,
        document_count=1,
    )
    if link is not None:
        links_repo.deactivate(link)
    links_repo.create(
        person_id=person.id,
        mention_id=mention.id,
        method="reviewer",
        score=None,
        rule_id="reviewer_split",
        rationale=notes or f"reviewer {reviewer} kept mentions separate",
    )
    return person


def _apply_er_decision(
    db: Session, item: ReviewItem, decision: str, reviewer: str, notes: str | None
) -> DecisionOutcome:
    ref = item.ref or {}
    run_id = _run_id_of(item)
    left = db.get(Mention, uuid.UUID(ref["left_mention_id"]))
    right = db.get(Mention, uuid.UUID(ref["right_mention_id"]))
    if left is None or right is None:
        raise NotFoundError("one of the pair's mentions no longer exists")
    links_repo = IdentityLinkRepository(db)
    left_link = links_repo.active_link_for_mention(left.id)
    right_link = links_repo.active_link_for_mention(right.id)
    if left_link is None or right_link is None:
        raise ConflictError(
            "pair mentions are not actively linked to persons (was the ER stage re-run?)"
        )
    left_person = db.get(Person, left_link.person_id)
    right_person = db.get(Person, right_link.person_id)
    score = ref.get("score")
    decisions_repo = ErDecisionRepository(db)
    affected: set[uuid.UUID] = set()
    detail: dict = {}

    if decision == "merge":
        if left_person.id == right_person.id:
            detail["note"] = "already same person; decision recorded"
            er_decision = "merge"
        else:
            moved = _merge_persons(
                db, left_person, right_person, score=score, reviewer=reviewer, notes=notes
            )
            detail["moved_links"] = moved
            detail["survivor_person_id"] = str(left_person.id)
            detail["absorbed_person_id"] = str(right_person.id)
            er_decision = "merge"
        affected |= {left_person.id, right_person.id}
    else:  # keep_separate
        if left_person.id == right_person.id:
            # They were merged since the item was queued -- split right out.
            new_person = _split_mention_to_new_person(db, right, run_id, reviewer, notes)
            detail["split_person_id"] = str(new_person.id)
            _refresh_person_counters(db, left_person)
            affected |= {left_person.id, new_person.id}
            er_decision = "split"
        else:
            left_person.review_status = "human_confirmed"
            right_person.review_status = "human_confirmed"
            affected |= {left_person.id, right_person.id}
            er_decision = "no_merge"

    decisions_repo.create(
        left_ref={"mention_id": str(left.id), "run_id": str(run_id)},
        right_ref={"mention_id": str(right.id), "run_id": str(run_id)},
        decision=er_decision,
        method="reviewer",
        score=score,
        rationale=notes or f"reviewer {reviewer}: {decision}",
    )
    db.flush()

    # docs/plan.md §14c SUSPICIOUS 5a: this decision may have made OTHER
    # open er_pair items moot (their mentions now share a cluster, or the
    # pair itself is a live hard conflict) -- close them so the adjudicator
    # never re-spends budget on a pair already settled here. Exclude THIS
    # item: it is still 'open' at this point (the caller flips it to
    # 'decided' right after this function returns) and must get the real
    # decision recorded below, not get raced into 'dismissed' first.
    from app.services.er.persist import close_superseded_er_pairs

    superseded = close_superseded_er_pairs(db, run_id, exclude_item_id=item.id)
    if superseded:
        detail["superseded_items"] = superseded

    recomputed = sorted(affected)
    recompute_for_persons(db, run_id, recomputed)
    return DecisionOutcome(
        item_id=item.id, decision=decision, recomputed_person_ids=recomputed, detail=detail
    )


def _apply_flag_audit_decision(
    db: Session, item: ReviewItem, decision: str, after: dict, reviewer: str
) -> DecisionOutcome:
    from app.repositories.exposure_flags import ExposureFlagRepository

    ref = item.ref or {}
    flag = ExposureFlagRepository(db).get(uuid.UUID(ref["exposure_flag_id"]))
    if flag is None:
        raise NotFoundError(f"exposure flag {ref.get('exposure_flag_id')} no longer exists")
    if decision == "confirm":
        flag.review_status = "human_confirmed"
    else:  # override
        flag.review_status = "human_overridden"
        if "exposed" in after:
            flag.exposed = bool(after["exposed"])
    db.flush()
    return DecisionOutcome(
        item_id=item.id, decision=decision, recomputed_person_ids=[], detail={}
    )


def decide_review_item(
    db: Session,
    item_id: uuid.UUID,
    *,
    decision: str,
    reviewer: str,
    notes: str | None = None,
    after: dict | None = None,
) -> DecisionOutcome:
    """Lock, validate, apply, record, project. Raises NotFoundError /
    ConflictError / ValidationFailedError; commits nothing (caller owns the
    transaction, per the repository convention)."""
    repo = ReviewRepository(db)
    item = repo.get_for_update(item_id)
    if item is None:
        raise NotFoundError(f"review item {item_id} not found")
    if item.status != "open":
        raise ConflictError(f"review item {item_id} is already {item.status}")

    allowed = {
        "extraction": _EXTRACTION_DECISIONS,
        "er_pair": _ER_DECISIONS,
        "flag_audit": _FLAG_DECISIONS,
    }[item.kind]
    if decision not in allowed:
        raise ValidationFailedError(
            f"decision {decision!r} is not valid for kind {item.kind!r} "
            f"(allowed: {sorted(allowed)})"
        )
    after = after or {}

    if item.kind == "extraction":
        outcome = _apply_extraction_decision(db, item, decision, after, reviewer, notes)
    elif item.kind == "er_pair":
        outcome = _apply_er_decision(db, item, decision, reviewer, notes)
    else:
        outcome = _apply_flag_audit_decision(db, item, decision, after, reviewer)

    item.status = "decided"
    repo.create_decision(
        review_item_id=item.id,
        decision=decision,
        reviewer=reviewer,
        before=outcome.detail.get("before"),
        after=after or None,
        notes=notes,
    )
    db.flush()
    logger.info(
        "review_decision_applied",
        item_id=str(item.id),
        kind=item.kind,
        decision=decision,
        recomputed_persons=[str(p) for p in outcome.recomputed_person_ids],
    )
    return outcome
