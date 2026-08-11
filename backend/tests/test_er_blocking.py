"""Unit tests for services/er/blocking.py: every block family fires on
its designed case, corpusgen's actual misspelling generator is covered
(the measured reason the typo block exists), and pair generation stays
subquadratic on 1000 synthetic mentions (docs/plan.md D5 / §12)."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from corpusgen.identities import misspell  # noqa: E402
from faker import Faker  # noqa: E402

from app.services.er.blocking import (  # noqa: E402
    generate_candidate_pairs,
    surname_deletion_keys,
)
from app.services.er.normalize import build_mention  # noqa: E402


def pair_reasons(result, id_a, id_b):
    return result.pairs.get(tuple(sorted((id_a, id_b))), set())


# --- block families --------------------------------------------------------


def test_strong_identifier_block_across_formats():
    a = build_mention("a", "D1", "Ana Cruz", elements=[("ssn", "512-33-7788")])
    b = build_mention("b", "D2", "Completely Different", elements=[("ssn", "512 33 7788")])
    result = generate_candidate_pairs([a, b])
    assert any(r.startswith("strong:ssn:") for r in pair_reasons(result, "a", "b"))


def test_phonetic_block_pairs_nickname_with_canonical():
    # No shared elements at all -- only the canonicalized phonetic key
    # can surface this pair ("ben" expands to benjamin before metaphone).
    a = build_mention("a", "D1", "Ben Lam")
    b = build_mention("b", "D2", "Benjamin Lam")
    result = generate_candidate_pairs([a, b])
    assert any(r.startswith("phonetic:") for r in pair_reasons(result, "a", "b"))


def test_initial_block_pairs_initial_with_full_given():
    a = build_mention("a", "D1", "B. Lam")
    b = build_mention("b", "D2", "Benjamin Lam")
    result = generate_candidate_pairs([a, b])
    assert any(r.startswith("initial:") for r in pair_reasons(result, "a", "b"))


def test_same_dob_block():
    # Different names, no shared identifiers -- only DOB can pair them
    # (the maiden-name safety net).
    a = build_mention("a", "D1", "Elizabeth Nguyen", elements=[("dob", "1970-02-11")])
    b = build_mention("b", "D2", "Elizabeth Tran", elements=[("dob", "1970-02-11")])
    result = generate_candidate_pairs([a, b])
    assert "dob:1970-02-11" in pair_reasons(result, "a", "b")


def test_unrelated_mentions_are_never_paired():
    a = build_mention("a", "D1", "Ana Cruz", elements=[("ssn", "111-22-3333")])
    b = build_mention("b", "D2", "Wei Zhang", elements=[("ssn", "444-55-6666")])
    assert generate_candidate_pairs([a, b]).pairs == {}


# --- typo block vs corpusgen's actual misspelling generator ----------------


def test_deletion_keys_cover_swap_drop_double():
    # misspell() applies exactly one of: adjacent swap, drop, double.
    # Each leaves at least one shared single-deletion key.
    assert surname_deletion_keys("walker") & surname_deletion_keys("wlaker")  # swap
    assert surname_deletion_keys("smith") & surname_deletion_keys("sith")  # drop
    assert surname_deletion_keys("hill") & surname_deletion_keys("hilll")  # double


def test_typo_block_covers_generated_misspellings_completely():
    """The measured design decision (see blocking.py docstring): single
    metaphone missed 30.7% of generated misspelled-surname keys and
    double metaphone still missed 29.0%, so the symmetric-delete key --
    100% coverage on the same measurement -- is the mechanism. This test
    re-runs that measurement against the real generator."""
    rng = random.Random(42)
    faker = Faker("en_US")
    Faker.seed(42)
    mentions = []
    expected_pairs = []
    for i in range(150):
        surname = faker.last_name()
        first = faker.first_name()
        a = build_mention(f"orig{i}", f"DA{i}", f"{first} {surname}")
        b = build_mention(f"typo{i}", f"DB{i}", f"{first} {misspell(rng, surname)}")
        mentions.extend([a, b])
        expected_pairs.append((f"orig{i}", f"typo{i}"))
    result = generate_candidate_pairs(mentions)
    missed = [
        pair for pair in expected_pairs
        if tuple(sorted(pair)) not in result.pairs
    ]
    assert not missed, f"typo block missed {len(missed)}/150: {missed[:5]}"


def test_nickname_variant_meets_misspelled_variant_across_kinds():
    # Cross-kind pair from one identity: nickname given + typo'd surname.
    # Deletion keys are emitted per CANONICAL initial, so "peggy" (-> m
    # for margaret) still meets "Margaret Hendreson".
    rng = random.Random(7)
    a = build_mention("a", "D1", "Peggy Henderson")
    b = build_mention("b", "D2", f"Margaret {misspell(rng, 'Henderson')}")
    result = generate_candidate_pairs([a, b])
    assert pair_reasons(result, "a", "b")


# --- subquadratic guarantee ------------------------------------------------


def test_blocking_is_subquadratic_on_1000_mentions():
    """1000 synthetic mentions (500 persons x 2 mentions, Faker names,
    per-person SSN/DOB): candidate pairs must be a tiny fraction of
    n^2/2 = 499,500 -- blocking, not scoring, is what makes 1M docs
    feasible (§12)."""
    rng = random.Random(42)
    faker = Faker("en_US")
    Faker.seed(42)
    mentions = []
    for p in range(500):
        first, last = faker.first_name(), faker.last_name()
        ssn = f"{rng.randint(100, 899):03d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}"
        dob = f"{rng.randint(1955, 2004)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        for m in range(2):
            mentions.append(
                build_mention(
                    f"p{p}m{m}", f"D{p}-{m}", f"{first} {last}",
                    elements=[("ssn", ssn), ("dob", dob)],
                )
            )
    result = generate_candidate_pairs(mentions)
    n = len(mentions)
    all_pairs = n * (n - 1) // 2
    assert result.stats.n_pairs < all_pairs * 0.05, (
        f"{result.stats.n_pairs} pairs is not << {all_pairs}"
    )
    # And blocking still finds every within-person pair (shared SSN).
    for p in range(500):
        assert (f"p{p}m0", f"p{p}m1") in result.pairs


def test_oversized_name_blocks_are_capped_but_strong_blocks_never():
    # 300 "John Smith" mentions with distinct SSNs: the phonetic block
    # exceeds the cap and is skipped; give 2 of them a shared SSN and the
    # strong block still pairs them (identical value is decisive).
    mentions = [
        build_mention(f"m{i}", f"D{i}", "John Smith", elements=[("ssn", f"{100 + i:03d}-11-2222")])
        for i in range(300)
    ]
    mentions.append(build_mention("dup", "D999", "John Smith", elements=[("ssn", "100-11-2222")]))
    result = generate_candidate_pairs(mentions, max_name_block_size=200)
    assert result.stats.oversized_blocks
    assert any(r.startswith("strong:ssn:") for r in pair_reasons(result, "m0", "dup"))


def test_blocking_is_deterministic_under_input_order():
    a = build_mention("a", "D1", "Ben Lam", elements=[("ssn", "111-22-3333")])
    b = build_mention("b", "D2", "Benjamin Lam", elements=[("ssn", "111-22-3333")])
    c = build_mention("c", "D3", "Wei Zhang", elements=[("dob", "1980-01-01")])
    forward = generate_candidate_pairs([a, b, c])
    backward = generate_candidate_pairs([c, b, a])
    assert forward.pairs == backward.pairs
