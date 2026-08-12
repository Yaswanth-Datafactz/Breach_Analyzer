"""Pure-function unit tests for services/accuracy.py's person-matching,
pairwise-ER arithmetic, per-category rollup, and error-class histogram --
no DB, no LLM calls, fake data only (docs/plan.md §10's methodology,
task 5's "person matching ... missed/split/wrongly_merged/hallucinated
each individually triggered and asserted").
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.services.accuracy import (
    FlagResultRow,
    ManifestIdentityGT,
    PersonResultRow,
    PredictedPersonAgg,
    _category_table,
    _error_class_histogram,
    _fuzzy_match,
    _pairwise_confusion,
    _person_metrics,
    _trap_forms,
    _trap_index,
    load_manifest_identities,
    match_persons,
    non_trap_plantings_by_doc,
)
from app.services.er.normalize import normalize_value


def gt(uid: str, name: str, dob: date | None = None, **strong: str) -> ManifestIdentityGT:
    return ManifestIdentityGT(
        person_uid=uid,
        full_name=name,
        dob=dob,
        name_values=frozenset({name}),
        strong_ids=strong,
        expected_categories=frozenset(),
    )


def pred(name: str, dob: date | None = None, **strong: str) -> PredictedPersonAgg:
    return PredictedPersonAgg(
        person_id=uuid.uuid4(),
        best_name=name,
        dob=dob,
        strong_ids={k: frozenset({v}) for k, v in strong.items()},
    )


# ---------------------------------------------------------------------------
# Stage 1: strong-identifier precedence (the SharedName worked example)
# ---------------------------------------------------------------------------


def test_strong_identifier_beats_name_similarity_for_shared_name():
    """Two DIFFERENT real people printed with the IDENTICAL name --
    name-similarity alone is a coin flip between them, but each carries a
    distinct SSN that must force the correct pairing (docs/plan.md §10's
    own precedence rule, and the exact SharedName scenario corpusgen
    plants -- corpusgen/scenarios.py's SharedName class)."""
    david_a = gt("P0001", "David Wilson", ssn="111223333")
    david_b = gt("P0002", "David Wilson", ssn="444556666")
    # Predicted persons printed with the SAME shared name but carrying
    # each real person's OWN (correctly-extracted) ssn -- if matching
    # relied on name alone these could trivially be swapped/tied.
    pred_a = pred("David Wilson", ssn="111223333")
    pred_b = pred("David Wilson", ssn="444556666")

    results = {r.manifest_person_uid: r for r in match_persons([pred_a, pred_b], [david_a, david_b])}

    assert results["P0001"].outcome == "matched"
    assert results["P0001"].matched_person_id == pred_a.person_id
    assert results["P0001"].match_basis == "exact_ssn"
    assert results["P0002"].outcome == "matched"
    assert results["P0002"].matched_person_id == pred_b.person_id
    assert results["P0002"].match_basis == "exact_ssn"


def test_precedence_prefers_ssn_over_email_when_both_present():
    # Deliberately mismatched email (as if two people's emails collided --
    # which corpus construction never actually produces, but the matcher
    # should still prefer the higher-precedence type when both are on
    # offer for the SAME pair) to prove ssn wins the match_basis label.
    identity = gt("P0001", "Priya Shah", ssn="555667777", email="priya@example.com")
    person = pred("Priya Shah", ssn="555667777", email="priya@example.com")
    results = match_persons([person], [identity])
    assert results[0].match_basis == "exact_ssn"


# ---------------------------------------------------------------------------
# Outcome taxonomy: missed / split / wrongly_merged / hallucinated
# ---------------------------------------------------------------------------


def test_missed_when_no_predicted_person_matches():
    identity = gt("P0001", "Nobody Found", ssn="000112222")
    results = match_persons([], [identity])
    assert len(results) == 1
    assert results[0].outcome == "missed"
    assert results[0].matched_person_id is None
    assert results[0].match_basis is None


def test_split_when_one_identity_evidence_lands_on_two_predicted_persons():
    """ER over-split: the SAME real person's mentions got clustered into
    two separate predicted persons, each independently carrying that
    person's ssn (e.g. attached via two different documents)."""
    identity = gt("P0001", "Jane Split", ssn="123456789")
    p1 = pred("Jane Split", ssn="123456789")
    p2 = pred("J. Split", ssn="123456789")

    results = match_persons([p1, p2], [identity])

    assert len(results) == 2
    assert {r.outcome for r in results} == {"split"}
    assert {r.matched_person_id for r in results} == {p1.person_id, p2.person_id}
    assert all(r.manifest_person_uid == "P0001" for r in results)


def test_wrongly_merged_when_one_predicted_person_carries_two_identities_strong_ids():
    """The shared-name failure (docs/plan.md §10's headline metric): ER
    incorrectly merged two DIFFERENT real people into one predicted
    person, which now holds both of their strong identifiers."""
    identity_a = gt("P0001", "Alpha Person", ssn="111111111")
    identity_b = gt("P0002", "Beta Person", email="beta@example.com")
    merged = PredictedPersonAgg(
        person_id=uuid.uuid4(),
        best_name="Alpha Person",
        dob=None,
        strong_ids={"ssn": frozenset({"111111111"}), "email": frozenset({"beta@example.com"})},
    )

    results = match_persons([merged], [identity_a, identity_b])

    outcomes = {r.manifest_person_uid: r.outcome for r in results}
    assert outcomes == {"P0001": "wrongly_merged", "P0002": "wrongly_merged"}
    assert all(r.matched_person_id == merged.person_id for r in results)


def test_hallucinated_when_predicted_person_matches_nothing():
    phantom = pred("Totally Fictional Person")
    results = match_persons([phantom], [])
    assert len(results) == 1
    assert results[0].outcome == "hallucinated"
    assert results[0].manifest_person_uid.startswith("__hallucinated__:")
    assert results[0].matched_person_id == phantom.person_id


# ---------------------------------------------------------------------------
# Stage 2: fuzzy name+DOB assignment over the leftover pool
# ---------------------------------------------------------------------------


def test_fuzzy_stage_matches_nickname_via_name_and_dob_when_no_strong_id():
    """A NicknameCluster-style document with a nickname printing and NO
    strong identifier attached in this chunk -- stage 1 has nothing to go
    on, so stage 2's name+DOB corroboration must catch it."""
    identity = ManifestIdentityGT(
        person_uid="P0001",
        full_name="Robert Nichols",
        dob=date(1980, 5, 1),
        name_values=frozenset({"Robert Nichols", "Bob Nichols"}),
        strong_ids={},
        expected_categories=frozenset(),
    )
    nickname_person = PredictedPersonAgg(
        person_id=uuid.uuid4(), best_name="Bob Nichols", dob=date(1980, 5, 1), strong_ids={}
    )

    results = match_persons([nickname_person], [identity])

    assert results[0].outcome == "matched"
    assert results[0].match_basis == "name_dob_fuzzy"
    assert results[0].matched_person_id == nickname_person.person_id


def test_fuzzy_stage_rejects_unrelated_names():
    identity = gt("P0001", "Alexandra Whitfield")
    unrelated = pred("Kevin Osei")
    results = match_persons([unrelated], [identity])
    outcomes = {r.outcome for r in results}
    assert outcomes == {"missed", "hallucinated"}


def test_fuzzy_match_is_greedy_and_deterministic_under_ties():
    """Two leftover predicted persons both plausibly matching one
    leftover identity -- greedy-by-descending-score must pick exactly
    one, never both, and never crash on the tie."""
    identity = gt("P0001", "Morgan Lee")
    p1 = pred("Morgan Lee")
    p2 = pred("Morgan Lee")
    matches = _fuzzy_match([p1, p2], [identity])
    assert len(matches) == 1
    assert matches[0][1] == "P0001"


# ---------------------------------------------------------------------------
# Pairwise ER arithmetic
# ---------------------------------------------------------------------------


def test_pairwise_confusion_arithmetic():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    true_of = {a: "P1", b: "P1", c: "P2", d: "P2"}
    cluster_x = uuid.uuid4()
    # Predicted correctly clusters a+b; incorrectly leaves c and d apart
    # (an er_split from the predicted side).
    pred_of = {a: cluster_x, b: cluster_x, c: uuid.uuid4(), d: uuid.uuid4()}

    tp, fp, fn = _pairwise_confusion(true_of, pred_of)

    assert (tp, fp, fn) == (1, 0, 1)


def test_pairwise_confusion_penalizes_overmerge_as_fp():
    a, b, c = (uuid.uuid4() for _ in range(3))
    true_of = {a: "P1", b: "P2", c: "P2"}
    cluster = uuid.uuid4()
    # Predicted wrongly merges ALL three into one cluster.
    pred_of = {a: cluster, b: cluster, c: cluster}

    tp, fp, fn = _pairwise_confusion(true_of, pred_of)

    # true pairs: (b,c) only -> tp=1; predicted pairs: (a,b),(a,c),(b,c)=3 -> fp=2; fn=0
    assert (tp, fp, fn) == (1, 2, 0)


# ---------------------------------------------------------------------------
# Person P/R/F1 + per-category table + error-class histogram
# ---------------------------------------------------------------------------


def test_person_metrics_precision_recall_formula():
    rows = [
        PersonResultRow("P1", uuid.uuid4(), "exact_ssn", "matched"),
        PersonResultRow("P2", None, None, "missed"),
        PersonResultRow("P3", uuid.uuid4(), "exact_ssn", "split"),
        PersonResultRow("P3", uuid.uuid4(), "exact_ssn", "split"),
    ]
    metrics = _person_metrics(rows, manifest_count=3, predicted_count=4)

    assert metrics["matched"] == 1
    assert metrics["missed"] == 1
    assert metrics["split"] == 1  # distinct manifest uids, not row count
    # _person_metrics rounds to 4dp (matches costs.py's convention), so
    # comparisons use an absolute tolerance wider than that rounding.
    assert metrics["precision"] == pytest.approx(1 / 4, abs=1e-4)
    assert metrics["recall"] == pytest.approx(1 / 3, abs=1e-4)
    assert metrics["f1"] == pytest.approx(2 * (1 / 4) * (1 / 3) / (1 / 4 + 1 / 3), abs=1e-4)


def test_person_metrics_vacuous_cases_do_not_divide_by_zero():
    assert _person_metrics([], manifest_count=0, predicted_count=0)["precision"] == 1.0
    assert _person_metrics([], manifest_count=0, predicted_count=0)["recall"] == 1.0


def test_category_table_counts_and_precision_recall():
    rows = [
        FlagResultRow("P1", "ssn", True, True, "tp", None),
        FlagResultRow("P1", "email", True, False, "fn", "missed_extraction"),
        FlagResultRow("P2", "ssn", False, True, "fp", "wrong_category"),
        FlagResultRow("P2", "email", False, False, "tn", None),
    ]
    table = {row["category"]: row for row in _category_table(rows)}

    assert table["ssn"]["tp"] == 1 and table["ssn"]["fp"] == 1
    assert table["ssn"]["precision"] == pytest.approx(0.5)
    assert table["email"]["fn"] == 1 and table["email"]["tn"] == 1
    assert table["email"]["precision"] is None  # no predicted-positive for email here
    assert table["dob"]["tp"] == 0
    assert table["dob"]["precision"] is None and table["dob"]["recall"] is None


def test_error_class_histogram_folds_person_and_flag_levels():
    """The SCHEMA NOTE case: split/wrongly_merged/missed/hallucinated have
    no error_class DB column on accuracy_person_results, so the histogram
    is where their classification actually lands."""
    person_rows = [
        PersonResultRow("P1", uuid.uuid4(), "x", "split"),
        PersonResultRow("P1", uuid.uuid4(), "x", "split"),
        PersonResultRow("P2", uuid.uuid4(), "x", "wrongly_merged"),
        PersonResultRow("P3", None, None, "missed"),
        PersonResultRow(f"__hallucinated__:{uuid.uuid4()}", uuid.uuid4(), None, "hallucinated"),
    ]
    flag_rows = [
        FlagResultRow("P5", "ssn", True, False, "fn", "ocr_failure"),
        FlagResultRow("P5", "email", False, True, "fp", "trap_fp"),
        FlagResultRow("P5", "phone", True, True, "tp", None),
    ]

    histogram = _error_class_histogram(person_rows, flag_rows, {"P3": "missed_extraction"})

    assert histogram["er_split"] == 2  # one per split ROW, both belong to the same manifest identity
    assert histogram["er_overmerge"] == 1
    assert histogram["missed_extraction"] == 1
    assert histogram["hallucinated"] == 1
    assert histogram["ocr_failure"] == 1
    assert histogram["trap_fp"] == 1
    assert "tp" not in histogram and None not in histogram


# ---------------------------------------------------------------------------
# Trap forms / trap index / manifest planting indexing
# ---------------------------------------------------------------------------


def test_trap_forms_covers_multiple_normalizations():
    forms = _trap_forms("384-05-3312")
    assert normalize_value("ssn", "384-05-3312") in forms
    assert normalize_value("credit_card", "384-05-3312") in forms


def test_trap_index_scopes_by_rel_path_and_skips_real_identities():
    manifest = {
        "documents": [
            {
                "filename": "D0001_invoice.xlsx",
                "plantings": [
                    {"person_uid": None, "element_type": "trap_order_number", "value": "384-05-3312"},
                ],
            },
            {
                "filename": "D0002_memo.pdf",
                "plantings": [
                    {"person_uid": "P0001", "element_type": "ssn", "value": "111-22-3333"},
                ],
            },
        ]
    }
    idx = _trap_index(manifest)
    assert normalize_value("ssn", "384-05-3312") in idx["D0001_invoice.xlsx"]
    assert "D0002_memo.pdf" not in idx  # no trap plantings there


def test_non_trap_plantings_by_doc_excludes_traps():
    manifest = {
        "documents": [
            {
                "filename": "D0001.pdf",
                "plantings": [
                    {"person_uid": "P0001", "element_type": "name", "value": "Alice A"},
                    {"person_uid": "P0001", "element_type": "ssn", "value": "111-22-3333"},
                    {"person_uid": None, "element_type": "trap_placeholder", "value": "XXX-XX-1234"},
                ],
            }
        ]
    }
    idx = non_trap_plantings_by_doc(manifest)
    assert idx["D0001.pdf"]["P0001"] == [("name", "Alice A"), ("ssn", "111-22-3333")]


def test_load_manifest_identities_derives_expected_categories():
    manifest = {
        "identities": [
            {
                "person_uid": "P0001",
                "canonical_name": "Alice A",
                "dob": "1990-01-01",
                "name_variants": [{"value": "A. A", "kind": "initials"}],
                "elements": {"ssn": "111-22-3333", "email": "a@example.com", "medical": "flu"},
            },
        ],
        "documents": [
            {
                "filename": "D0001.pdf",
                "plantings": [
                    {"person_uid": "P0001", "element_type": "ssn", "value": "111-22-3333"},
                    {"person_uid": "P0001", "element_type": "medical", "value": "flu"},
                    {"person_uid": "P0001", "element_type": "name", "value": "Alice A"},
                    # email is on the identity but never actually PLANTED
                    # anywhere -- must NOT show up as expected.
                ],
            }
        ],
    }
    identities = load_manifest_identities(manifest)
    assert len(identities) == 1
    identity = identities[0]
    # expected_categories is planting-derived (only ssn/medical were ever
    # actually planted in a document); strong_ids is NOT -- it reads
    # straight off the identity's always-fully-populated `elements` dict
    # (every identity has a full synthetic profile from corpusgen/
    # identities.py, only a subset of which any given scenario plants).
    # This is harmless for matching: a predicted person can only ever
    # HOLD a value that was actually extracted from planted text, so an
    # unplanted strong id here simply never has a counterpart to match.
    assert identity.expected_categories == frozenset({"ssn", "medical"})
    assert {"Alice A", "A. A"} <= identity.name_values
    assert identity.strong_ids["ssn"] == normalize_value("ssn", "111-22-3333")
    assert identity.strong_ids["email"] == normalize_value("email", "a@example.com")
