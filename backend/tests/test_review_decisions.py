"""services/review.py decision projections (docs/plan.md §4: decisions
project back): er_pair merge -> identity_links (append-only flip+insert) +
er_decisions method=reviewer + exposure recompute; keep_separate records
no-merge (and SPLITS with an inactive row when the mentions had been
merged); extraction corrections re-run exposure; double-decide -> 409
semantics. Fresh scenario per test -- decisions mutate clustering state.
Real :5434 Postgres (UC2 convention)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.db.models import ErDecision, IdentityLink, Person, PiiElement, ReviewItem
from app.db.session import SessionLocal
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.review import ReviewRepository
from app.services.er.persist import run_er_stage
from app.services.exposure import compute_exposure
from app.services.review import decide_review_item
from tests.er_scenario import build_scenario, teardown_scenario


@pytest.fixture()
def ready():
    db = SessionLocal()
    scenario = build_scenario(db)
    run_er_stage(db, scenario.run_id)
    compute_exposure(db, scenario.run_id)
    db.commit()
    try:
        yield db, scenario
    finally:
        teardown_scenario(db, scenario)
        db.close()


def _active_link(db, mention_id) -> IdentityLink | None:
    return db.scalar(
        select(IdentityLink).where(
            IdentityLink.mention_id == mention_id, IdentityLink.active.is_(True)
        )
    )


def _links_of_mention(db, mention_id) -> list[IdentityLink]:
    return list(
        db.scalars(
            select(IdentityLink)
            .where(IdentityLink.mention_id == mention_id)
            .order_by(IdentityLink.created_at.asc(), IdentityLink.id.asc())
        )
    )


def _gray_item_with_m4(db, scenario, other_mention_key: str) -> ReviewItem:
    m4 = str(scenario.mentions["m4"])
    other = str(scenario.mentions[other_mention_key])
    items, _ = ReviewRepository(db).list_items(kind="er_pair", status="open", run_id=scenario.run_id)
    for item in items:
        pair = {item.ref["left_mention_id"], item.ref["right_mention_id"]}
        if pair == {m4, other}:
            return item
    raise AssertionError(f"no open gray item for m4 vs {other_mention_key}")


def test_merge_moves_links_append_only_and_recomputes(ready):
    db, scenario = ready
    m1, m4 = scenario.mentions["m1"], scenario.mentions["m4"]
    robert_person = _active_link(db, m1).person_id
    m4_person_before = _active_link(db, m4).person_id
    assert m4_person_before != robert_person
    item = _gray_item_with_m4(db, scenario, "m1")

    outcome = decide_review_item(
        db, item.id, decision="merge", reviewer="tester", notes="same person, nickname"
    )
    db.commit()

    survivor_id = _active_link(db, m4).person_id
    assert survivor_id == _active_link(db, m1).person_id
    # Append-only: every mention that MOVED (the absorbed side -- which side
    # that is depends on the pair's sorted ref order) gained a reviewer row;
    # nothing was deleted.
    all_mentions = [scenario.mentions[k] for k in ("m1", "m2", "m5", "m4")]
    moved = [mid for mid in all_mentions if len(_links_of_mention(db, mid)) == 2]
    assert len(moved) in (1, 3)  # m4 alone, or the Robert trio, moved
    for mid in moved:
        links = _links_of_mention(db, mid)
        assert [link.active for link in links] == [False, True]
        assert links[1].method == "reviewer"
        assert links[1].rule_id == "reviewer_merge"

    survivor = db.get(Person, survivor_id)
    assert survivor.mention_count == 4
    assert survivor.review_status == "human_confirmed"
    absorbed = db.get(Person, m4_person_before if survivor_id == robert_person else robert_person)
    assert absorbed.mention_count == 0  # hidden from the exposure table

    reviewer_rows = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.method == "reviewer",
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
            )
        )
    )
    assert [r.decision for r in reviewer_rows] == ["merge"]
    assert set(outcome.recomputed_person_ids) == {survivor_id, absorbed.id}
    # Signal-attached elements followed the identity to the survivor...
    cred = db.get(PiiElement, scenario.elements["cred_dump"])
    assert cred.signals["er_join_person_id"] == str(survivor_id)
    # ...so the recompute leaves the absorbed person with no flags and the
    # survivor with the full set.
    assert ExposureFlagRepository(db).flags_for_person(absorbed.id) == []
    survivor_categories = {
        f.category for f in ExposureFlagRepository(db).flags_for_person(survivor_id)
    }
    assert survivor_categories == {"ssn", "dob", "credit_card", "credentials"}
    item = db.get(ReviewItem, item.id)
    assert item.status == "decided"


def test_keep_separate_records_no_merge_without_link_changes(ready):
    db, scenario = ready
    m2, m4 = scenario.mentions["m2"], scenario.mentions["m4"]
    item = _gray_item_with_m4(db, scenario, "m2")
    links_before = {mid: len(_links_of_mention(db, mid)) for mid in (m2, m4)}

    decide_review_item(db, item.id, decision="keep_separate", reviewer="tester")
    db.commit()

    assert _active_link(db, m2).person_id != _active_link(db, m4).person_id
    for mid in (m2, m4):
        assert len(_links_of_mention(db, mid)) == links_before[mid]  # no new rows
        assert db.get(Person, _active_link(db, mid).person_id).review_status == "human_confirmed"
    rows = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.method == "reviewer",
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
            )
        )
    )
    assert [r.decision for r in rows] == ["no_merge"]


def test_keep_separate_after_merge_splits_with_inactive_row(ready):
    db, scenario = ready
    m1, m4 = scenario.mentions["m1"], scenario.mentions["m4"]
    decide_review_item(
        db, _gray_item_with_m4(db, scenario, "m1").id, decision="merge", reviewer="tester"
    )
    db.commit()
    merged_person = _active_link(db, m4).person_id
    assert merged_person == _active_link(db, m1).person_id
    m4_links_before = len(_links_of_mention(db, m4))

    # The auditor flags the merge as wrong: a new er_pair item on the SAME
    # person's two mentions; keep_separate must SPLIT m4 back out.
    item = ReviewRepository(db).create_item(
        kind="er_pair",
        ref={
            "run_id": str(scenario.run_id),
            "left_mention_id": str(m1),
            "right_mention_id": str(m4),
            "score": 0.79,
        },
        reason="flag_audit_contradiction",
    )
    db.commit()
    outcome = decide_review_item(db, item.id, decision="keep_separate", reviewer="tester")
    db.commit()

    new_person = _active_link(db, m4).person_id
    assert new_person != merged_person
    links = _links_of_mention(db, m4)
    # The split appended one row (the plan's inactive-row-on-split ledger):
    # every earlier link is now inactive, the reviewer_split row is active.
    assert len(links) == m4_links_before + 1
    assert [link.active for link in links] == [False] * (len(links) - 1) + [True]
    assert links[-1].rule_id == "reviewer_split"
    split_rows = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.method == "reviewer", ErDecision.decision == "split"
            )
        )
    )
    assert any(
        r.left_ref["run_id"] == str(scenario.run_id) for r in split_rows
    )
    assert set(outcome.recomputed_person_ids) == {merged_person, new_person}


def test_extraction_correction_recomputes_exposure(ready):
    db, scenario = ready
    m1 = scenario.mentions["m1"]
    robert_id = _active_link(db, m1).person_id
    flags = {f.category for f in ExposureFlagRepository(db).flags_for_person(robert_id)}
    assert "credit_card" in flags

    item = ReviewRepository(db).create_item(
        kind="extraction",
        ref={
            "pii_element_id": str(scenario.elements["card_valid_m1"]),
            "run_id": str(scenario.run_id),
        },
        reason="flag_audit_contradiction",
    )
    db.commit()
    outcome = decide_review_item(
        db,
        item.id,
        decision="correct",
        reviewer="tester",
        after={"validation_status": "format_only"},
    )
    db.commit()

    element = db.get(PiiElement, scenario.elements["card_valid_m1"])
    assert element.validation_status == "format_only"
    assert element.detector == "reviewer"
    assert robert_id in outcome.recomputed_person_ids
    flags_after = {f.category for f in ExposureFlagRepository(db).flags_for_person(robert_id)}
    assert "credit_card" not in flags_after  # its only evidence was corrected away
    assert "ssn" in flags_after  # untouched categories survive


def test_attach_decision_binds_orphan_and_recomputes(ready):
    db, scenario = ready
    m1 = scenario.mentions["m1"]
    robert_id = _active_link(db, m1).person_id
    item = db.scalar(
        select(ReviewItem).where(
            ReviewItem.kind == "extraction",
            ReviewItem.ref["pii_element_id"].astext == str(scenario.elements["phone_orphan"]),
        )
    )
    decide_review_item(
        db,
        item.id,
        decision="attach",
        reviewer="tester",
        after={"person_id": str(robert_id)},
    )
    db.commit()

    element = db.get(PiiElement, scenario.elements["phone_orphan"])
    assert element.signals["reviewer_attached_person_id"] == str(robert_id)
    flags = {
        f.category: f for f in ExposureFlagRepository(db).flags_for_person(robert_id)
    }
    assert "phone" in flags
    evidence = ExposureFlagRepository(db).evidence_for_flag(flags["phone"].id)
    assert [e.pii_element_id for e in evidence] == [scenario.elements["phone_orphan"]]


def test_second_decision_conflicts(ready):
    db, scenario = ready
    item = _gray_item_with_m4(db, scenario, "m1")
    decide_review_item(db, item.id, decision="keep_separate", reviewer="tester")
    db.commit()
    with pytest.raises(ConflictError):
        decide_review_item(db, item.id, decision="merge", reviewer="tester2")
    db.rollback()


def test_unknown_item_and_invalid_decision(ready):
    db, scenario = ready
    from app.core.errors import NotFoundError, ValidationFailedError

    with pytest.raises(NotFoundError):
        decide_review_item(db, uuid.uuid4(), decision="merge", reviewer="t")
    db.rollback()
    item = _gray_item_with_m4(db, scenario, "m1")
    with pytest.raises(ValidationFailedError):
        decide_review_item(db, item.id, decision="correct", reviewer="t")
    db.rollback()
