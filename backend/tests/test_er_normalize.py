"""Unit tests for services/er/normalize.py (docs/plan.md §15: ER
normalize is named core business logic).

The nickname-map coverage test imports the REAL corpusgen module -- the
manifest's nickname variants are generated from corpusgen's NICKNAMES, so
"our curated map covers every corpusgen entry" is exactly the exam the
accuracy harness will re-run at scale."""

import sys
from pathlib import Path

# corpusgen lives at the repo root, one level above backend/ (pytest's
# pythonpath covers backend only).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from corpusgen.identities import NICKNAMES  # noqa: E402

from app.services.er.normalize import (  # noqa: E402
    build_mention,
    given_name_canonicals,
    normalize_name,
    normalize_value,
)


# --- nickname canonicalization ---------------------------------------------


def test_nickname_map_covers_every_corpusgen_entry():
    """Every (canonical, nickname) pair the corpus generator can plant
    must canonicalize back -- a gap here is a guaranteed missed link on a
    planted NicknameCluster variant."""
    for canonical, nicknames in NICKNAMES.items():
        for nickname in nicknames:
            assert canonical.casefold() in given_name_canonicals(nickname.casefold()), (
                f"nickname map misses corpusgen entry {nickname!r} -> {canonical!r}"
            )


def test_two_nicknames_of_same_canonical_are_compatible():
    # "bob" and "rob" never appear in each other's lists -- they meet at
    # the canonical "robert". Set intersection is the compatibility rule.
    assert given_name_canonicals("bob") & given_name_canonicals("rob")


def test_ambiguous_nickname_keeps_all_canonicals():
    assert {"samantha", "samuel"} <= given_name_canonicals("sam")


def test_canonical_name_maps_to_itself():
    assert given_name_canonicals("robert") == frozenset({"robert"})


# --- name normalization ----------------------------------------------------


def test_last_first_reorder():
    name = normalize_name("Lam, Benjamin")
    assert name.full == "benjamin lam"
    assert name.given == "benjamin"
    assert name.surname == "lam"
    assert name.was_reordered
    # And the reordered form normalizes identically to the plain form.
    assert name.full == normalize_name("Benjamin Lam").full


def test_initial_detection_not_expansion():
    name = normalize_name("B. Lam")
    assert name.given == "b"
    assert name.given_is_initial
    assert name.surname == "lam"
    # A bare initial gets no nickname expansion -- "b" could be Benjamin
    # or Barbara; expansion is blocking/features' attempt, not a fact.
    assert name.given_canonicals == frozenset({"b"})


def test_punctuation_case_and_whitespace():
    assert normalize_name("  ROBERT   O'Brien\tJr. ").full == "robert obrien"
    assert normalize_name("Mary-Jane Watson").tokens == ("mary", "jane", "watson")


def test_single_token_name_has_no_surname():
    name = normalize_name("Benjamin")
    assert name.given == "benjamin"
    assert name.surname is None


# --- value normalizers -----------------------------------------------------


def test_ssn_digits_only():
    assert normalize_value("ssn", "769-35-5574") == "769355574"
    assert normalize_value("ssn", "769 35 5574") == "769355574"
    assert normalize_value("ssn_last4", "*-5574") == "5574"


def test_phone_digits_only_with_country_code():
    assert normalize_value("phone", "(913) 555-0142") == "9135550142"
    assert normalize_value("phone", "+1 913-555-0142") == "9135550142"


def test_email_lowercase():
    assert (
        normalize_value("email", " Benjamin.Lam699@Example.ORG ")
        == "benjamin.lam699@example.org"
    )
    assert normalize_value("email", "mailto:a@example.com") == "a@example.com"


def test_dob_formats_converge_to_iso():
    for raw in ("1972-03-04", "03/04/1972", "March 4, 1972", "Mar 4, 1972"):
        assert normalize_value("dob", raw) == "1972-03-04", raw


def test_credit_card_and_account_digits_only():
    assert normalize_value("credit_card", "4539 1488 0343 6467") == "4539148803436467"
    assert normalize_value("financial_account", "12-3456789") == "123456789"


def test_employee_id_alnum_upper():
    assert normalize_value("employee_id", " e12345 ") == "E12345"


# --- mention builder -------------------------------------------------------


def test_build_mention_normalizes_and_folds_dob():
    mention = build_mention(
        "m1",
        "D0001",
        name_raw="Lam, Benjamin",
        elements=[("ssn", "769-35-5574"), ("dob", "03/04/1972"), ("email", "A@Example.COM")],
    )
    assert mention.name is not None and mention.name.full == "benjamin lam"
    assert mention.dob == "1972-03-04"
    assert mention.elements["ssn"] == frozenset({"769355574"})
    assert mention.elements["email"] == frozenset({"a@example.com"})


def test_build_mention_without_name():
    mention = build_mention("m2", "D0002", elements=[("ssn", "769-35-5574")])
    assert mention.name is None
    assert mention.dob is None
