"""DB-backed tests for services/accuracy.py's score_accuracy end to end:
pairwise-ER ground-truth resolution against real mention/element rows,
per-flag P/R including trap exclusion, and error classification for each
of the 6 classes with a concrete fixture per class (task 5). Runs against
the real Postgres on :5434 (UC2's DB-test convention), building each
scenario directly through the existing repositories -- no LLM calls, no
fake-adapter pipeline drive (that's scripts/run_accuracy_eval.py's job
and is exercised by the live verification run instead).

Every test creates its own ProcessingRun and cleans it up in `finally`;
deleting the run cascades to every document/passage/mention/pii_element/
person/identity_link/exposure_flag/flag_evidence row it owns (db/
models.py's ondelete="CASCADE" chain), so no other cleanup is needed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.db.models import ProcessingRun
from app.db.session import SessionLocal
from app.repositories.documents import DocumentRepository
from app.repositories.exposure_flags import ExposureFlagRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.mentions import MentionRepository
from app.repositories.persons import PersonRepository
from app.repositories.pii_elements import PiiElementRepository
from app.repositories.runs import ProcessingRunRepository
from app.services.accuracy import score_accuracy
from app.services.er.normalize import normalize_value


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _cleanup_run(db, run_id) -> None:
    db.rollback()
    db.execute(delete(ProcessingRun).where(ProcessingRun.id == run_id))
    db.commit()


def _sha() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _document(db, run, rel_path: str, *, is_image_based: bool = False, file_class: str = "pdf_digital"):
    document = DocumentRepository(db).create(
        run_id=run.id,
        sha256=_sha(),
        original_filename=rel_path,
        rel_path=rel_path,
        byte_size=100,
        file_class=file_class,
        source_kind="corpus",
    )
    document.is_image_based = is_image_based
    document.status = "done"
    db.flush()
    return document


def _passage(db, document, text: str, *, ocr: bool = False):
    from app.repositories.passages import PassageRepository

    return PassageRepository(db).create(
        document_id=document.id, seq=0, kind="page", locator={"page": 1}, text=text, ocr=ocr
    )


def _mention(db, document, passage, name_raw: str, *, dob=None):
    return MentionRepository(db).create(
        document_id=document.id,
        passage_id=passage.id,
        name_raw=name_raw,
        detector="llm_tier1",
        dob=dob,
        confidence=0.9,
    )


def _element(
    db,
    document,
    passage,
    *,
    element_type: str,
    value_raw: str,
    mention_id=None,
    validation_status: str = "valid",
    signals: dict | None = None,
):
    normalized = normalize_value(element_type, value_raw)
    return PiiElementRepository(db).create(
        document_id=document.id,
        passage_id=passage.id,
        element_type=element_type,
        value_raw=value_raw,
        value_normalized=normalized[:500],
        char_start=0,
        char_end=len(value_raw),
        detector="llm_tier1",
        validation_status=validation_status,
        mention_id=mention_id,
        confidence=0.9,
        signals=signals,
    )


def _person(db, run, best_name: str, *, dob=None):
    return PersonRepository(db).create(run_id=run.id, best_name=best_name, dob=dob, mention_count=1, document_count=1)


def _link(db, person, mention):
    return IdentityLinkRepository(db).create(
        person_id=person.id, mention_id=mention.id, method="rule", score=1.0, rule_id="test"
    )


def _flag(db, person, category: str, *, exposed: bool = True):
    return ExposureFlagRepository(db).create_flag(person_id=person.id, category=category, exposed=exposed, confidence=0.9)


def _evidence(db, flag, element, document, passage):
    return ExposureFlagRepository(db).add_evidence(
        exposure_flag_id=flag.id, pii_element_id=element.id, document_id=document.id, passage_id=passage.id, snippet="..."
    )


def _identity(uid: str, name: str, dob: str = "1985-06-15", **elements: str) -> dict:
    return {"person_uid": uid, "canonical_name": name, "dob": dob, "name_variants": [], "elements": elements}


def _doc(filename: str, plantings: list[dict]) -> dict:
    return {"filename": filename, "plantings": plantings}


def _planting(uid: str | None, element_type: str, value: str) -> dict:
    return {"person_uid": uid, "element_type": element_type, "value": value}


def _manifest(identities: list[dict], documents: list[dict]) -> dict:
    return {"seed": 1, "profile": "test", "identities": identities, "documents": documents}


def _result_by_uid(scoring_result, uid: str):
    return [r for r in scoring_result.person_results if r.manifest_person_uid == uid]


def _flag_row(scoring_result, uid: str, category: str):
    return next(
        r for r in scoring_result.flag_results if r.manifest_person_uid == uid and r.category == category
    )


# ---------------------------------------------------------------------------
# Pairwise ER: ground-truth resolution against real mention/element rows
# ---------------------------------------------------------------------------


def test_pairwise_er_resolves_ground_truth_and_scores_correctly(db):
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        # Person 1: two documents, two mentions, correctly clustered under
        # ONE predicted person (true positive pairwise link).
        doc1 = _document(db, run, "D1.pdf")
        passage1 = _passage(db, doc1, "Nora Ellison, SSN 111-22-3333")
        mention1 = _mention(db, doc1, passage1, "Nora Ellison")
        _element(db, doc1, passage1, element_type="ssn", value_raw="111-22-3333", mention_id=mention1.id)

        doc2 = _document(db, run, "D2.pdf")
        passage2 = _passage(db, doc2, "Nora Ellison, SSN 111-22-3333")
        mention2 = _mention(db, doc2, passage2, "Nora Ellison")
        _element(db, doc2, passage2, element_type="ssn", value_raw="111-22-3333", mention_id=mention2.id)

        person1 = _person(db, run, "Nora Ellison")
        _link(db, person1, mention1)
        _link(db, person1, mention2)

        # Person 2: one document, one mention, WRONGLY clustered together
        # with person 1's mentions is what we're testing does NOT happen --
        # here it's correctly its own person (a true negative-pair region).
        doc3 = _document(db, run, "D3.pdf")
        passage3 = _passage(db, doc3, "Miguel Santos, SSN 444-55-6666")
        mention3 = _mention(db, doc3, passage3, "Miguel Santos")
        _element(db, doc3, passage3, element_type="ssn", value_raw="444-55-6666", mention_id=mention3.id)
        person2 = _person(db, run, "Miguel Santos")
        _link(db, person2, mention3)
        db.commit()

        manifest = _manifest(
            [
                _identity("P_NORA", "Nora Ellison", ssn="111223333"),
                _identity("P_MIGUEL", "Miguel Santos", ssn="444556666"),
            ],
            [
                _doc("D1.pdf", [_planting("P_NORA", "name", "Nora Ellison"), _planting("P_NORA", "ssn", "111-22-3333")]),
                _doc("D2.pdf", [_planting("P_NORA", "name", "Nora Ellison"), _planting("P_NORA", "ssn", "111-22-3333")]),
                _doc("D3.pdf", [_planting("P_MIGUEL", "name", "Miguel Santos"), _planting("P_MIGUEL", "ssn", "444-55-6666")]),
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")
        pairwise = result.metrics["pairwise_er"]

        assert pairwise["mentions_total"] == 3
        assert pairwise["mentions_ground_truth_resolved"] == 3
        assert pairwise["scored_mentions"] == 3
        # Only true pair is (mention1, mention2); predicted correctly
        # clusters exactly that pair and nothing else -> perfect P/R.
        assert pairwise["tp"] == 1
        assert pairwise["fp"] == 0
        assert pairwise["fn"] == 0
        assert pairwise["precision"] == 1.0
        assert pairwise["recall"] == 1.0
        assert pairwise["f1"] == 1.0
    finally:
        _cleanup_run(db, run.id)


# ---------------------------------------------------------------------------
# Error classification -- one concrete fixture per class (task 5)
# ---------------------------------------------------------------------------


def test_missed_extraction_error_class(db):
    """A category planted on a NON-image document but never extracted at
    all: the predicted person has zero elements/flags of that type."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        document = _document(db, run, "D_missext.pdf", is_image_based=False)
        passage = _passage(db, document, "Meg Miller, SSN 555-66-7777")
        mention = _mention(db, document, passage, "Meg Miller")
        ssn_element = _element(db, document, passage, element_type="ssn", value_raw="555-66-7777", mention_id=mention.id)
        person = _person(db, run, "Meg Miller")
        _link(db, person, mention)
        flag = _flag(db, person, "ssn")
        _evidence(db, flag, ssn_element, document, passage)
        # medical was PLANTED (see manifest below) but never extracted --
        # no element, no flag -- on a digital (non-image) document.
        db.commit()

        manifest = _manifest(
            [_identity("P_MISSEXT", "Meg Miller", ssn="555667777", medical="hypertension")],
            [
                _doc(
                    "D_missext.pdf",
                    [
                        _planting("P_MISSEXT", "name", "Meg Miller"),
                        _planting("P_MISSEXT", "ssn", "555-66-7777"),
                        _planting("P_MISSEXT", "medical", "hypertension"),
                    ],
                )
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        assert _result_by_uid(result, "P_MISSEXT")[0].outcome == "matched"
        medical_row = _flag_row(result, "P_MISSEXT", "medical")
        assert medical_row.outcome == "fn"
        assert medical_row.error_class == "missed_extraction"
        assert result.metrics["error_class_histogram"]["missed_extraction"] == 1
    finally:
        _cleanup_run(db, run.id)


def test_ocr_failure_error_class(db):
    """Same shape as missed_extraction, but the planting's ONLY document
    is image-based in this run -- OCR degradation is the more likely
    explanation, and error classification must say so."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        document = _document(db, run, "D_ocrfail.pdf", is_image_based=True)
        passage = _passage(db, document, "Omar Reyes SSN 888-99-0000 [ocr noise]", ocr=True)
        mention = _mention(db, document, passage, "Omar Reyes")
        ssn_element = _element(db, document, passage, element_type="ssn", value_raw="888-99-0000", mention_id=mention.id)
        person = _person(db, run, "Omar Reyes")
        _link(db, person, mention)
        flag = _flag(db, person, "ssn")
        _evidence(db, flag, ssn_element, document, passage)
        db.commit()

        manifest = _manifest(
            [_identity("P_OCRFAIL", "Omar Reyes", ssn="888990000", drivers_license="D1234567")],
            [
                _doc(
                    "D_ocrfail.pdf",
                    [
                        _planting("P_OCRFAIL", "name", "Omar Reyes"),
                        _planting("P_OCRFAIL", "ssn", "888-99-0000"),
                        _planting("P_OCRFAIL", "drivers_license", "D1234567"),
                    ],
                )
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        dl_row = _flag_row(result, "P_OCRFAIL", "drivers_license")
        assert dl_row.outcome == "fn"
        assert dl_row.error_class == "ocr_failure"
        assert result.metrics["error_class_histogram"]["ocr_failure"] == 1
    finally:
        _cleanup_run(db, run.id)


def test_er_split_error_class(db):
    """One manifest identity's evidence lands on TWO predicted persons
    (ER over-split): both carry that identity's real ssn."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        doc1 = _document(db, run, "D_split1.pdf")
        passage1 = _passage(db, doc1, "Priya Nair, SSN 222-33-4444")
        mention1 = _mention(db, doc1, passage1, "Priya Nair")
        element1 = _element(db, doc1, passage1, element_type="ssn", value_raw="222-33-4444", mention_id=mention1.id)
        person1 = _person(db, run, "Priya Nair")
        _link(db, person1, mention1)
        flag1 = _flag(db, person1, "ssn")
        _evidence(db, flag1, element1, doc1, passage1)

        doc2 = _document(db, run, "D_split2.pdf")
        passage2 = _passage(db, doc2, "P. Nair, SSN 222-33-4444")
        mention2 = _mention(db, doc2, passage2, "P. Nair")
        element2 = _element(db, doc2, passage2, element_type="ssn", value_raw="222-33-4444", mention_id=mention2.id)
        person2 = _person(db, run, "P. Nair")  # ER incorrectly kept this SEPARATE from person1
        _link(db, person2, mention2)
        flag2 = _flag(db, person2, "ssn")
        _evidence(db, flag2, element2, doc2, passage2)
        db.commit()

        manifest = _manifest(
            [_identity("P_SPLIT", "Priya Nair", ssn="222334444")],
            [
                _doc("D_split1.pdf", [_planting("P_SPLIT", "name", "Priya Nair"), _planting("P_SPLIT", "ssn", "222-33-4444")]),
                _doc("D_split2.pdf", [_planting("P_SPLIT", "name", "P. Nair"), _planting("P_SPLIT", "ssn", "222-33-4444")]),
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        split_rows = _result_by_uid(result, "P_SPLIT")
        assert len(split_rows) == 2
        assert {r.outcome for r in split_rows} == {"split"}
        assert {r.matched_person_id for r in split_rows} == {person1.id, person2.id}
        assert result.metrics["person"]["split"] == 1  # ONE manifest identity affected
        assert result.metrics["error_class_histogram"]["er_split"] == 2  # TWO result rows
    finally:
        _cleanup_run(db, run.id)


def test_er_overmerge_error_class(db):
    """One predicted person's linked mentions carry TWO DIFFERENT
    manifest identities' ssns (ER over-merge -- the shared-name
    failure)."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        doc1 = _document(db, run, "D_merge1.pdf")
        passage1 = _passage(db, doc1, "Sam Carter, SSN 100-20-3000")
        mention1 = _mention(db, doc1, passage1, "Sam Carter")
        element1 = _element(db, doc1, passage1, element_type="ssn", value_raw="100-20-3000", mention_id=mention1.id)

        doc2 = _document(db, run, "D_merge2.pdf")
        passage2 = _passage(db, doc2, "Sam Carter, SSN 900-80-7000")
        mention2 = _mention(db, doc2, passage2, "Sam Carter")
        element2 = _element(db, doc2, passage2, element_type="ssn", value_raw="900-80-7000", mention_id=mention2.id)

        merged_person = _person(db, run, "Sam Carter")
        _link(db, merged_person, mention1)
        _link(db, merged_person, mention2)
        flag1 = _flag(db, merged_person, "ssn")
        _evidence(db, flag1, element1, doc1, passage1)
        _evidence(db, flag1, element2, doc2, passage2)
        db.commit()

        manifest = _manifest(
            [
                _identity("P_MERGE_A", "Sam Carter", ssn="100203000"),
                _identity("P_MERGE_B", "Sam Carter", ssn="900807000"),
            ],
            [
                _doc("D_merge1.pdf", [_planting("P_MERGE_A", "name", "Sam Carter"), _planting("P_MERGE_A", "ssn", "100-20-3000")]),
                _doc("D_merge2.pdf", [_planting("P_MERGE_B", "name", "Sam Carter"), _planting("P_MERGE_B", "ssn", "900-80-7000")]),
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        outcomes = {r.manifest_person_uid: r.outcome for r in result.person_results}
        assert outcomes == {"P_MERGE_A": "wrongly_merged", "P_MERGE_B": "wrongly_merged"}
        assert all(r.matched_person_id == merged_person.id for r in result.person_results)
        assert result.metrics["wrongly_merged_manifest_identities"] == 2
        assert result.metrics["wrongly_merged_predicted_persons"] == 1
        assert result.metrics["error_class_histogram"]["er_overmerge"] == 2
    finally:
        _cleanup_run(db, run.id)


def test_trap_fp_vs_wrong_category_error_class(db):
    """One matched person with TWO bogus (non-expected) exposed flags:
    one whose evidence value equals a manifest trap planting at the SAME
    document (-> trap_fp, and counted in the run-wide trap scorecard),
    one that is simply wrong with no trap behind it (-> wrong_category).
    Directly proves per-flag P/R correctly EXCLUDES trap leakage from
    ordinary wrong_category accounting (task 5's "incl. trap exclusion")."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        document = _document(db, run, "D_trap.pdf")
        passage = _passage(
            db, document, "Tara Trapp, SSN 300-40-5000. Order #384-05-3312. Also: mystery-card 4111111111111111"
        )
        mention = _mention(db, document, passage, "Tara Trapp")
        ssn_element = _element(db, document, passage, element_type="ssn", value_raw="300-40-5000", mention_id=mention.id)
        # Leaked trap: a tier-1 mis-extraction that escaped the trap
        # downgrade (validation_status='valid', no trap_reason signal) --
        # exactly what "trap leakage" looks like on a real run.
        leaked_trap_element = _element(
            db, document, passage, element_type="credit_card", value_raw="4111111111111111", mention_id=mention.id
        )
        # Ordinary wrong category: some other bogus value with NO trap
        # planting behind it anywhere in this document.
        bogus_element = _element(
            db, document, passage, element_type="passport", value_raw="999999999", mention_id=mention.id
        )
        person = _person(db, run, "Tara Trapp")
        _link(db, person, mention)

        ssn_flag = _flag(db, person, "ssn")
        _evidence(db, ssn_flag, ssn_element, document, passage)
        credit_card_flag = _flag(db, person, "credit_card")
        _evidence(db, credit_card_flag, leaked_trap_element, document, passage)
        passport_flag = _flag(db, person, "passport")
        _evidence(db, passport_flag, bogus_element, document, passage)
        db.commit()

        manifest = _manifest(
            [_identity("P_TRAP", "Tara Trapp", ssn="300405000")],  # no credit_card/passport ever planted
            [
                _doc(
                    "D_trap.pdf",
                    [
                        _planting("P_TRAP", "name", "Tara Trapp"),
                        _planting("P_TRAP", "ssn", "300-40-5000"),
                        _planting(None, "trap_card_invalid", "4111111111111111"),
                    ],
                )
            ],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        assert _result_by_uid(result, "P_TRAP")[0].outcome == "matched"

        credit_card_row = _flag_row(result, "P_TRAP", "credit_card")
        assert credit_card_row.outcome == "fp"
        assert credit_card_row.error_class == "trap_fp"

        passport_row = _flag_row(result, "P_TRAP", "passport")
        assert passport_row.outcome == "fp"
        assert passport_row.error_class == "wrong_category"

        histogram = result.metrics["error_class_histogram"]
        assert histogram["trap_fp"] == 1
        assert histogram["wrong_category"] == 1

        trap_scorecard = result.metrics["trap_scorecard"]
        assert trap_scorecard["total_trap_plantings"] == 1
        assert trap_scorecard["trap_derived_fp_flags"] == 1
        assert trap_scorecard["by_category"] == {"credit_card": 1}

        # per-category table: ssn is a clean TP; credit_card/passport both
        # FP (reducing precision) but NEITHER is silently folded away --
        # the trap-derived one is separately visible via the scorecard.
        per_category = {row["category"]: row for row in result.metrics["per_category"]}
        assert per_category["ssn"]["tp"] == 1
        assert per_category["credit_card"]["fp"] == 1
        assert per_category["passport"]["fp"] == 1
    finally:
        _cleanup_run(db, run.id)


def test_trap_scorecard_is_run_wide_not_scoped_to_matched_persons(db):
    """A trap leaking onto a person accuracy scoring can't even match
    (here: a predicted person built ENTIRELY from a mis-read trap, with
    no real identity behind it at all -- hallucinated) must still show
    up in the trap scorecard. Proves task 2d's "run-wide, not scoped to
    matched persons" design choice actually matters."""
    run = ProcessingRunRepository(db).create(config_snapshot={"corpus_path": "/test", "test": True})
    db.commit()
    try:
        document = _document(db, run, "D_phantom.pdf")
        passage = _passage(db, document, "Some routine text. Order #384-05-3312 confirmed.")
        # A phantom mention manufactured from trap context (should not
        # happen in a well-behaved system -- this fixture exists
        # specifically to prove the scorecard catches it if it does).
        phantom_mention = _mention(db, document, passage, "Order Desk")
        leaked_element = _element(
            db, document, passage, element_type="ssn", value_raw="384-05-3312", mention_id=phantom_mention.id
        )
        phantom_person = _person(db, run, "Order Desk")
        _link(db, phantom_person, phantom_mention)
        flag = _flag(db, phantom_person, "ssn")
        _evidence(db, flag, leaked_element, document, passage)
        db.commit()

        manifest = _manifest(
            [],  # no real identities at all in this tiny fixture
            [_doc("D_phantom.pdf", [_planting(None, "trap_order_number", "384-05-3312")])],
        )

        result = score_accuracy(db, run.id, manifest, manifest_path="test.json")

        assert result.person_results[0].outcome == "hallucinated"  # confirms it's OUTSIDE compute_flag_results' scope
        assert result.flag_results == []  # matched-only scope -- nothing here

        trap_scorecard = result.metrics["trap_scorecard"]
        assert trap_scorecard["trap_derived_fp_flags"] == 1  # but the run-wide scorecard still sees it
        assert trap_scorecard["by_category"] == {"ssn": 1}
    finally:
        _cleanup_run(db, run.id)
