"""Unit tests for services/er/features.py + scoring.py -- the hard
constraints (docs/plan.md D5) and the variant-similarity behavior,
exercised against corpusgen's ACTUAL variant generator (fixed seed), not
hand-picked strings: what the generator plants is what production must
score correctly."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from corpusgen.identities import build_variants, misspell  # noqa: E402
from rapidfuzz.distance import JaroWinkler  # noqa: E402

from app.services.er.features import (  # noqa: E402
    SURNAME_TYPO_THRESHOLD,
    compute_features,
)
from app.services.er.normalize import build_mention  # noqa: E402
from app.services.er.scoring import score_pair  # noqa: E402

AUTO = 0.85  # config er_auto_link_threshold default
GRAY_FLOOR = 0.45  # config er_distinct_threshold default


def scored(a, b):
    return score_pair(a.mention_id, b.mention_id, compute_features(a, b))


# --- hard constraint 1: conflicting strong identifiers ---------------------


def test_conflicting_ssn_forces_zero():
    """The SharedName trap: identical names, different SSNs."""
    a = build_mention("a", "D1", "Jordan Reyes", elements=[("ssn", "123-45-6789")])
    b = build_mention("b", "D2", "Jordan Reyes", elements=[("ssn", "987-65-4321")])
    pair = scored(a, b)
    assert pair.score == 0.0
    assert pair.hard_reason == "hard_conflict"
    assert "ssn" in pair.features["conflicting_strong_types"]


def test_conflict_wins_over_shared_identifier():
    """Sharing an email cannot rescue a pair that conflicts on SSN --
    conservative by design (a wrong merge poisons the exposure table)."""
    a = build_mention(
        "a", "D1", "Jordan Reyes",
        elements=[("ssn", "123-45-6789"), ("email", "x@example.com")],
    )
    b = build_mention(
        "b", "D2", "Jordan Reyes",
        elements=[("ssn", "987-65-4321"), ("email", "x@example.com")],
    )
    pair = scored(a, b)
    assert pair.score == 0.0
    assert pair.hard_reason == "hard_conflict"


def test_same_ssn_different_format_is_shared_not_conflicting():
    a = build_mention("a", "D1", "Jordan Reyes", elements=[("ssn", "123-45-6789")])
    b = build_mention("b", "D2", "Jordan Reyes", elements=[("ssn", "123 45 6789")])
    pair = scored(a, b)
    assert pair.hard_reason is None
    assert pair.score >= AUTO


# --- hard constraints 2/3: name-only caps ----------------------------------


def test_identical_name_alone_stays_below_gray_floor():
    """Name-only similarity with zero corroboration can never reach the
    adjudicator, let alone merge -- the structural SharedName guarantee."""
    a = build_mention("a", "D1", "Jordan Reyes")
    b = build_mention("b", "D2", "Jordan Reyes")
    pair = scored(a, b)
    assert pair.hard_reason == "name_only_cap"
    assert pair.score < GRAY_FLOOR


def test_nickname_variant_alone_lands_in_gray_band_never_auto():
    """A curated nickname hit without corroboration is exactly the case
    D5 sends to the adjudicator: above the distinct floor, below auto."""
    a = build_mention("a", "D1", "Robert Chen")
    b = build_mention("b", "D2", "Bob Chen")
    pair = scored(a, b)
    assert pair.features["nickname_hit"]
    assert pair.hard_reason == "name_variant_cap"
    assert GRAY_FLOOR < pair.score < AUTO


def test_nickname_with_shared_identifier_auto_links():
    a = build_mention("a", "D1", "Robert Chen", elements=[("phone", "(913) 555-0142")])
    b = build_mention("b", "D2", "Bob Chen", elements=[("phone", "913-555-0142")])
    pair = scored(a, b)
    assert pair.hard_reason is None
    assert pair.score >= AUTO


def test_initials_variant_alone_gray_never_auto():
    a = build_mention("a", "D1", "B. Lam")
    b = build_mention("b", "D2", "Benjamin Lam")
    pair = scored(a, b)
    assert pair.features["initials_compatible"]
    assert pair.hard_reason == "name_variant_cap"
    assert pair.score < AUTO


def test_dob_conflict_pushes_identical_name_to_zero():
    a = build_mention("a", "D1", "Jordan Reyes", elements=[("dob", "1961-01-31")])
    b = build_mention("b", "D2", "Jordan Reyes", elements=[("dob", "1975-06-02")])
    pair = scored(a, b)
    assert pair.features["dob_conflict"]
    assert pair.score < GRAY_FLOOR


# --- maiden name: candidate variant, never an auto-match -------------------


def test_maiden_surname_change_is_candidate_not_match():
    a = build_mention("a", "D1", "Elizabeth Nguyen")
    b = build_mention("b", "D2", "Elizabeth Tran")  # maiden variant
    pair = scored(a, b)
    assert pair.features["surname_change_candidate"]
    # Name evidence alone: nowhere near a merge.
    assert pair.score < GRAY_FLOOR


def test_maiden_variant_merges_via_shared_strong_identifier():
    a = build_mention("a", "D1", "Elizabeth Nguyen", elements=[("email", "e@example.org")])
    b = build_mention("b", "D2", "Elizabeth Tran", elements=[("email", "E@example.org")])
    pair = scored(a, b)
    assert pair.features["surname_change_candidate"]
    assert pair.hard_reason is None
    assert pair.score >= AUTO


# --- misspelling similarity vs corpusgen's actual generator ----------------


def test_generated_misspellings_clear_the_surname_typo_threshold():
    """SURNAME_TYPO_THRESHOLD must sit BELOW every similarity the real
    generator produces on surnames of length >= 4 (measured floor 0.830)
    and ABOVE genuinely different surnames. Length-3 surnames are the
    documented exception (see next test): an adjacent swap on "Lam"
    scores 0.556 -- below ANY threshold that still separates different
    surnames -- and those pairs link via identifiers instead."""
    rng = random.Random(42)
    surnames = [
        "Nguyen", "Henderson", "Walker", "Johnson", "Smith", "Hill",
        "Gonzalez", "Peterson", "Meyer", "Shaffer", "Pacheco", "Dudley",
    ]
    for surname in surnames:
        for _ in range(10):
            typo = misspell(rng, surname)
            similarity = JaroWinkler.normalized_similarity(
                surname.casefold(), typo.casefold()
            )
            assert similarity >= SURNAME_TYPO_THRESHOLD, (surname, typo, similarity)
    # Different-surname pairs stay below: the threshold separates typo
    # from maiden-style change.
    assert JaroWinkler.normalized_similarity("tran", "nguyen") < SURNAME_TYPO_THRESHOLD


def test_short_surname_swap_still_links_via_identifier():
    """The length-3 exception measured above: "Lam" -> "Lma" falls below
    the typo threshold, so the pair reads as a surname-change candidate --
    and still auto-links once a strong identifier corroborates, which is
    how every planted misspelling doc actually links in the corpus."""
    a = build_mention("a", "D1", "Benjamin Lam", elements=[("ssn", "769-35-5574")])
    b = build_mention("b", "D2", "Benjamin Lma", elements=[("ssn", "769-35-5574")])
    pair = scored(a, b)
    assert pair.features["surname_similarity"] < SURNAME_TYPO_THRESHOLD
    assert pair.hard_reason is None
    assert pair.score >= AUTO


def test_misspelled_surname_pair_scores_above_auto_with_shared_identifier():
    rng = random.Random(42)
    typo = misspell(rng, "Henderson")
    a = build_mention("a", "D1", "Margaret Henderson", elements=[("ssn", "321-54-9876")])
    b = build_mention("b", "D2", f"Margaret {typo}", elements=[("ssn", "321-54-9876")])
    pair = scored(a, b)
    assert pair.score >= AUTO
    # Without the identifier the same pair is name-only -> capped.
    a2 = build_mention("a2", "D1", "Margaret Henderson")
    b2 = build_mention("b2", "D2", f"Margaret {typo}")
    pair2 = scored(a2, b2)
    assert pair2.hard_reason == "name_only_cap"
    assert pair2.score < GRAY_FLOOR


def test_every_corpusgen_variant_kind_scores_auto_with_shared_identifier():
    """The end-to-end variant exam: every variant kind build_variants can
    emit must auto-link to its canonical form once a strong identifier
    corroborates -- exactly how NicknameCluster docs link in the corpus."""
    rng = random.Random(42)
    variants = build_variants(rng, "Margaret", "Henderson", maiden="Walker")
    kinds = {v.kind for v in variants}
    assert kinds == {"nickname", "maiden", "initials", "misspelling", "order_variant"}
    canonical = build_mention(
        "c", "D1", "Margaret Henderson", elements=[("email", "mh@example.com")]
    )
    for variant in variants:
        other = build_mention(
            f"v-{variant.kind}", "D2", variant.value,
            elements=[("email", "mh@example.com")],
        )
        pair = scored(canonical, other)
        assert pair.hard_reason is None, (variant.kind, pair.hard_reason)
        assert pair.score >= AUTO, (variant.kind, variant.value, pair.score)


# --- ssn_last4 bridge (PartialIdentifiers) ---------------------------------


def test_last4_compatible_corroborates_and_conflict_penalizes():
    full = build_mention("a", "D1", "Ravi Patel", elements=[("ssn", "512-33-7788")])
    last4_ok = build_mention("b", "D2", "Ravi Patel", elements=[("ssn_last4", "7788")])
    last4_bad = build_mention("c", "D3", "Ravi Patel", elements=[("ssn_last4", "1234")])
    ok = scored(full, last4_ok)
    assert ok.features["ssn_last4_compatible"]
    assert ok.hard_reason is None  # corroborated -> no name-only cap
    bad = scored(full, last4_bad)
    assert bad.features["ssn_last4_conflict"]
    assert bad.score < ok.score
    assert bad.score < GRAY_FLOOR
