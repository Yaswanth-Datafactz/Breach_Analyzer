"""services/er/persist.py over the fixture mention set (tests/er_scenario):
persons/links/decisions/gray-queue counts, best-name + alias shaping,
SharedName hard conflicts, TRANSITIVE enemy blocking, employee_id key
persistence, and stage idempotence. Real :5434 Postgres (UC2 convention)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ErDecision, IdentityLink, Mention, Person, ReviewItem
from app.db.session import SessionLocal
from app.services.er.persist import get_gray_pairs, run_er_stage
from tests.er_scenario import build_scenario, teardown_scenario


@pytest.fixture(scope="module")
def stage():
    db = SessionLocal()
    scenario = build_scenario(db)
    result = run_er_stage(db, scenario.run_id)
    db.commit()
    try:
        yield db, scenario, result
    finally:
        teardown_scenario(db, scenario)
        db.close()


def _person_of(db, mention_id):
    link = db.scalar(
        select(IdentityLink).where(
            IdentityLink.mention_id == mention_id, IdentityLink.active.is_(True)
        )
    )
    assert link is not None, f"mention {mention_id} has no active link"
    return link.person_id


def test_counts(stage):
    db, scenario, result = stage
    # Clusters: Robert{m1,m2,m5} + shared-name{m3} + gray{m4} + one Dana
    # pair + one Dana singleton = 5 persons over 8 mentions (which Dana
    # pair auto-links first is a tie broken by uuid order; the OTHER union
    # is always refused by the enemy registry, so counts are stable).
    assert result.mentions == 8
    assert result.persons == 5
    assert result.identity_links == 8
    assert result.er_decisions == result.scored_pairs > 0
    assert result.gray_items == 4  # m4 vs m1/m2/m5/m3
    assert result.blocked_by_conflict == 1
    assert result.hard_conflicts == 4  # m1/m2/m5 each vs m3, plus Dana e1-e3

    persons = list(db.scalars(select(Person).where(Person.run_id == scenario.run_id)))
    assert len(persons) == 5
    assert sum(p.mention_count for p in persons) == 8


def test_exact_ssn_auto_link_and_person_shape(stage):
    db, scenario, _ = stage
    p1 = _person_of(db, scenario.mentions["m1"])
    assert p1 == _person_of(db, scenario.mentions["m2"])
    assert p1 == _person_of(db, scenario.mentions["m5"])
    person = db.get(Person, p1)
    assert person.best_name == "Robert Fournier"  # most frequent form (2 vs 1)
    alias_names = {a["name"]: a["kind"] for a in person.aliases or []}
    assert alias_names.get("Bob Fournier") == "nickname"
    assert person.dob is not None and person.dob.isoformat() == "1987-03-14"
    assert person.mention_count == 3
    assert person.document_count == 3
    assert float(person.er_confidence) >= 0.85  # weakest auto link in cluster

    link = db.scalar(
        select(IdentityLink).where(
            IdentityLink.mention_id == scenario.mentions["m2"], IdentityLink.active.is_(True)
        )
    )
    assert link.method == "rule"
    assert link.rule_id.startswith("exact_")
    assert "ssn" in link.rule_id
    assert link.rationale  # every link carries rationale (D5)


def test_shared_name_never_merges(stage):
    db, scenario, _ = stage
    assert _person_of(db, scenario.mentions["m3"]) != _person_of(db, scenario.mentions["m1"])
    # And the refusal is a first-class er_decisions record, not silence.
    rows = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
                ErDecision.decision == "no_merge",
            )
        )
    )
    hard = [r for r in rows if "hard conflict" in (r.rationale or "")]
    assert hard, "hard-conflict pairs must be recorded as no_merge decisions"
    assert all(float(r.score) == 0.0 for r in hard)


def test_enemies_never_merge_transitively(stage):
    db, scenario, result = stage
    # e1-e2 share an email, e2-e3 share a phone; e1-e3 carry different SSNs.
    # Transitivity would put e1 and e3 in one person -- must be refused.
    assert _person_of(db, scenario.mentions["e1"]) != _person_of(db, scenario.mentions["e3"])
    assert result.blocked_by_conflict >= 1
    refused = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
                ErDecision.rationale.like("%transitively join%"),
            )
        )
    )
    assert refused, "the refused union must leave an er_decisions trail"


def test_gray_band_queued_for_adjudicator(stage):
    db, scenario, result = stage
    items = get_gray_pairs(db, scenario.run_id)
    assert len(items) == result.gray_items
    m4 = str(scenario.mentions["m4"])
    m4_items = [
        i for i in items if m4 in (i.ref.get("left_mention_id"), i.ref.get("right_mention_id"))
    ]
    assert m4_items, "the nickname+dob pair must reach the adjudicator queue"
    for item in m4_items:
        assert item.kind == "er_pair"
        assert item.status == "open"
        assert item.reason == "er_gray_band"
        assert 0.45 < item.ref["score"] < 0.85
        assert item.ref["left_person_id"] and item.ref["right_person_id"]
    # escalations are also er_decisions rows
    escalated = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
                ErDecision.decision == "escalate",
            )
        )
    )
    assert len(escalated) == result.gray_items


def test_employee_id_keys_persisted_on_mention(stage):
    db, scenario, _ = stage
    mention = db.get(Mention, scenario.mentions["m1"])
    assert mention.features.get("employee_id") == ["E12345"]


def test_rerun_is_idempotent_and_replaces_stage_output(stage):
    db, scenario, first = stage
    second = run_er_stage(db, scenario.run_id)
    db.commit()
    assert second.wiped_persons == first.persons
    assert second.persons == first.persons
    assert second.identity_links == first.identity_links
    assert second.er_decisions == first.er_decisions
    assert second.gray_items == first.gray_items
    # No duplicate open queue rows and no duplicate rule decisions.
    open_items = list(
        db.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == "er_pair",
                ReviewItem.status == "open",
                ReviewItem.ref["run_id"].astext == str(scenario.run_id),
            )
        )
    )
    assert len(open_items) == second.gray_items
    decisions = list(
        db.scalars(
            select(ErDecision).where(
                ErDecision.left_ref["run_id"].astext == str(scenario.run_id),
                ErDecision.method == "rule",
            )
        )
    )
    assert len(decisions) == second.er_decisions
