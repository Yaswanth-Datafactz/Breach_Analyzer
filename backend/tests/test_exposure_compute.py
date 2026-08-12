"""services/exposure.py over the fixture scenario: category flags with
evidence, trap/invalid exclusion, the §4 no-flag-without-evidence invariant
(raises), unattached-PII join-key attachment and review parking, and
recompute stability. Real :5434 Postgres (UC2 convention)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ExposureFlag, FlagEvidence, Person, PiiElement, ReviewItem
from app.db.session import SessionLocal
from app.repositories.exposure_flags import ExposureFlagRepository
from app.services.er.persist import run_er_stage
from app.services.exposure import (
    EvidenceInvariantViolation,
    compute_exposure,
)
from tests.er_scenario import build_scenario, teardown_scenario


@pytest.fixture(scope="module")
def computed():
    db = SessionLocal()
    scenario = build_scenario(db)
    run_er_stage(db, scenario.run_id)
    db.commit()
    result = compute_exposure(db, scenario.run_id)
    db.commit()
    try:
        yield db, scenario, result
    finally:
        teardown_scenario(db, scenario)
        db.close()


def _robert(db, scenario) -> Person:
    # TWO persons print as "Robert Fournier" (the SharedName pair, kept
    # apart by design) -- the real cluster is the 3-mention one.
    return db.scalar(
        select(Person)
        .where(
            Person.run_id == scenario.run_id,
            Person.best_name == "Robert Fournier",
            Person.mention_count == 3,
        )
    )


def _flags_by_category(db, person_id) -> dict[str, ExposureFlag]:
    return {f.category: f for f in ExposureFlagRepository(db).flags_for_person(person_id)}


def test_flags_and_evidence(computed):
    db, scenario, result = computed
    robert = _robert(db, scenario)
    flags = _flags_by_category(db, robert.id)
    assert set(flags) == {"ssn", "dob", "credit_card", "credentials"}
    assert all(f.exposed for f in flags.values())

    # ssn: m1 + m2 + m5 elements + the join-attached cred-dump SSN = 4 rows.
    ssn_evidence = ExposureFlagRepository(db).evidence_for_flag(flags["ssn"].id)
    assert len(ssn_evidence) == 4
    # Confidence = max element composite; checksum-valid tier-0 default 0.95.
    assert float(flags["ssn"].confidence) == pytest.approx(0.95)
    for evidence in ssn_evidence:
        assert evidence.passage_id is not None and evidence.snippet

    assert result.flags > 0 and result.evidence > 0


def test_traps_and_invalid_checksums_never_become_evidence(computed):
    db, scenario, _ = computed
    for key in ("ssn_trap_m1", "card_invalid_m1"):
        rows = list(
            db.scalars(
                select(FlagEvidence).where(
                    FlagEvidence.pii_element_id == scenario.elements[key]
                )
            )
        )
        assert rows == [], f"{key} (non-valid) must never back a flag"
    robert = _robert(db, scenario)
    card_flag = _flags_by_category(db, robert.id)["credit_card"]
    assert len(ExposureFlagRepository(db).evidence_for_flag(card_flag.id)) == 1


def test_unattached_elements_attach_via_er_join_keys(computed):
    db, scenario, result = computed
    robert = _robert(db, scenario)
    assert result.attached_via_join == 2  # cred password + cred-dump ssn
    for key in ("cred_dump", "ssn_dump"):
        element = db.get(PiiElement, scenario.elements[key])
        assert element.signals["er_join_person_id"] == str(robert.id)
        assert element.signals["er_join_key"].split(":")[0] in ("employee_id", "ssn")
    cred_flag = _flags_by_category(db, robert.id)["credentials"]
    evidence = ExposureFlagRepository(db).evidence_for_flag(cred_flag.id)
    assert [e.pii_element_id for e in evidence] == [scenario.elements["cred_dump"]]


def test_unattachable_element_parks_for_review(computed):
    db, scenario, result = computed
    assert result.parked_unattached == 1
    item = db.scalar(
        select(ReviewItem).where(
            ReviewItem.kind == "extraction",
            ReviewItem.ref["pii_element_id"].astext == str(scenario.elements["phone_orphan"]),
        )
    )
    assert item is not None
    assert item.reason == "unattached_pii"
    assert item.status == "open"
    # And it produced no flag anywhere.
    rows = list(
        db.scalars(
            select(FlagEvidence).where(
                FlagEvidence.pii_element_id == scenario.elements["phone_orphan"]
            )
        )
    )
    assert rows == []


def test_recompute_is_stable(computed):
    db, scenario, first = computed
    again = compute_exposure(db, scenario.run_id)
    db.commit()
    assert again.flags == first.flags
    assert again.evidence == first.evidence
    assert again.parked_unattached == 0  # already parked; never duplicated
    items = list(
        db.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == "extraction",
                ReviewItem.ref["run_id"].astext == str(scenario.run_id),
            )
        )
    )
    assert len(items) == 1


def test_parked_item_auto_dismissed_once_element_attaches(computed):
    db, scenario, _ = computed
    # Simulate a deeper extraction pass attributing the orphan phone to a
    # mention (tier-1 attaching a tier-0 hit): the open unattached_pii item
    # must be dismissed by the next run-level pass, with a recorded system
    # decision -- never silently deleted, never left as queue noise.
    element = db.get(PiiElement, scenario.elements["phone_orphan"])
    element.mention_id = scenario.mentions["m4"]
    db.commit()

    result = compute_exposure(db, scenario.run_id)
    db.commit()
    assert result.auto_dismissed == 1
    item = db.scalar(
        select(ReviewItem).where(
            ReviewItem.kind == "extraction",
            ReviewItem.ref["pii_element_id"].astext == str(scenario.elements["phone_orphan"]),
        )
    )
    assert item.status == "dismissed"
    decisions = item.decisions
    assert len(decisions) == 1
    assert decisions[0].decision == "auto_attached"
    assert decisions[0].reviewer == "system"
    # And the element now backs a phone flag on its mention's person.
    from app.db.models import IdentityLink

    link = db.scalar(
        select(IdentityLink).where(
            IdentityLink.mention_id == scenario.mentions["m4"], IdentityLink.active.is_(True)
        )
    )
    flags = _flags_by_category(db, link.person_id)
    assert "phone" in flags


def test_evidence_invariant_raises_when_violated(computed):
    db, scenario, _ = computed
    robert = _robert(db, scenario)
    # Simulate a rogue writer: a human-status flag with zero evidence rows
    # (auto flags without evidence are cleaned up; human ones are preserved
    # and therefore MUST be caught by the final invariant check).
    rogue = ExposureFlag(
        person_id=robert.id,
        category="passport",
        exposed=True,
        confidence=0.9,
        review_status="human_confirmed",
    )
    db.add(rogue)
    db.flush()
    with pytest.raises(EvidenceInvariantViolation):
        compute_exposure(db, scenario.run_id)
    db.rollback()
