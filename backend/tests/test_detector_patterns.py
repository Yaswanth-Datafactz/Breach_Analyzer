"""Pattern-level tests for the tier-0 regex library: each compiled
pattern matches the shapes corpusgen generates (group 1 = the value,
span exact) and refuses near-miss shapes. Cross-pattern precedence and
trap downgrades are covered in test_detector_tier0.py."""

import pytest

from app.services.detectors import patterns


def _only_match(pattern, text):
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, f"expected exactly one match in {text!r}, got {len(matches)}"
    return matches[0]


def _assert_value(pattern, text, value):
    m = _only_match(pattern, text)
    assert m.group(1) == value
    assert text[m.start(1) : m.end(1)] == value


# --- SSN --------------------------------------------------------------------


def test_ssn_dashed_matches_with_exact_span():
    _assert_value(patterns.SSN_DASHED, "SSN 523-41-8722 on file.", "523-41-8722")


@pytest.mark.parametrize(
    "text",
    [
        "serial 1523-41-8722",  # digit run continues left
        "code 523-41-87221",  # digit run continues right
        "id 523-41-8722-9",  # dash run continues right
        "phone 312-555-0142",  # 3-3-4 is a phone shape, not 3-2-4
    ],
)
def test_ssn_dashed_rejects_embedded_and_wrong_grouping(text):
    assert patterns.SSN_DASHED.search(text) is None


def test_ssn_spaced_matches():
    _assert_value(patterns.SSN_SPACED, "number 523 41 8722 was confirmed", "523 41 8722")


def test_ssn_spaced_rejects_longer_spaced_series():
    assert patterns.SSN_SPACED.search("readings 15 523 41 8722 22 end") is None


def test_ssn_undashed_requires_ssn_marker():
    _assert_value(patterns.SSN_UNDASHED, "Please confirm that SSN 523418722 today.", "523418722")
    _assert_value(patterns.SSN_UNDASHED, "Member SSN | 523418722", "523418722")
    # Same 9 digits without the marker: too generic to type as SSN.
    assert patterns.SSN_UNDASHED.search("the number 523418722 appears") is None
    # A closer competing label must win by construction (tight gap):
    assert patterns.SSN_UNDASHED.search("SSN unavailable; passport 731945082") is None


def test_ssn_last4_contextual_all_corpus_phrasings():
    # The exact templates.py sentence shapes + the claim-table label.
    _assert_value(
        patterns.SSN_LAST4,
        "For verification we matched the Social Security number on file ending in 8722.",
        "8722",
    )
    _assert_value(
        patterns.SSN_LAST4, "The SSN ending 8722 was used to confirm the account holder.", "8722"
    )
    _assert_value(patterns.SSN_LAST4, "SSN (last 4) | 8722", "8722")
    _assert_value(patterns.SSN_LAST4, "ssn last four digits: 8722", "8722")


def test_ssn_last4_needs_both_markers():
    assert patterns.SSN_LAST4.search("the code ending in 8722") is None  # no SSN marker
    assert patterns.SSN_LAST4.search("the SSN 8722 was entered") is None  # no last-4 marker
    assert patterns.SSN_LAST4.search("SSN ending 523-41-8722") is None  # full SSN, not a last-4


# --- credit card ------------------------------------------------------------


def test_card_grouped_16_space_and_dash():
    _assert_value(
        patterns.CARD_GROUPED_16, "card 4111 1111 1111 1111 on file", "4111 1111 1111 1111"
    )
    _assert_value(
        patterns.CARD_GROUPED_16, "card 4111-1111-1111-1111 on file", "4111-1111-1111-1111"
    )


def test_card_grouped_16_rejects_mixed_separators():
    assert patterns.CARD_GROUPED_16.search("num 4111 1111-1111 1111 end") is None


def test_card_grouped_15_amex_grouping():
    _assert_value(patterns.CARD_GROUPED_15, "amex 3782 822463 10005 billed", "3782 822463 10005")


@pytest.mark.parametrize("digits", ["4222222222222", "4111111111111111", "4111111111111111000"])
def test_card_contiguous_13_16_19(digits):
    _assert_value(patterns.CARD_CONTIGUOUS, f"pan {digits} end", digits)


@pytest.mark.parametrize("digits", ["411111111111", "41111111111111110000"])  # 12 / 20 digits
def test_card_contiguous_rejects_out_of_range_lengths(digits):
    assert patterns.CARD_CONTIGUOUS.search(f"pan {digits} end") is None


def test_card_grouped_cannot_start_inside_an_ssn():
    # The lookbehind guard: the SSN's trailing 4 digits must not seed a
    # phantom grouped card.
    text = "123-45-6789 4111 1111 1111 1111"
    m = _only_match(patterns.CARD_GROUPED_16, text)
    assert m.group(1) == "4111 1111 1111 1111"


# --- phone ------------------------------------------------------------------


def test_phone_paren_form():
    _assert_value(patterns.PHONE_PAREN, "call (415) 555-0137 today", "(415) 555-0137")
    _assert_value(patterns.PHONE_PAREN, "fax (312)555-0163 now", "(312)555-0163")


def test_phone_separated_forms():
    _assert_value(patterns.PHONE_SEPARATED, "at 415-555-0137, thanks", "415-555-0137")
    _assert_value(patterns.PHONE_SEPARATED, "at 415.555.0137, thanks", "415.555.0137")
    _assert_value(patterns.PHONE_SEPARATED, "at 415 555 0137, thanks", "415 555 0137")
    _assert_value(patterns.PHONE_SEPARATED, "at +1 415-555-0137, thanks", "+1 415-555-0137")


def test_phone_separated_rejects_mixed_separators_and_bad_nanp():
    assert patterns.PHONE_SEPARATED.search("at 415-555.0137, thanks") is None
    assert patterns.PHONE_SEPARATED.search("at 115-555-0137, thanks") is None  # area starts 1
    assert patterns.PHONE_SEPARATED.search("at 415-155-0137, thanks") is None  # exchange starts 1


def test_phone_patterns_do_not_match_an_ssn():
    assert patterns.PHONE_PAREN.search("SSN 523-41-8722 on file") is None
    assert patterns.PHONE_SEPARATED.search("SSN 523-41-8722 on file") is None


# --- email ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "maria.alvarez12@example.net",
        "j_ortiz+hr@example.org",
        "dana.whitfield@meridianbenefits.example",
    ],
)
def test_email_matches(value):
    _assert_value(patterns.EMAIL, f"Confirmation was sent to {value}.", value)


def test_email_rejects_bare_at_fragments():
    assert patterns.EMAIL.search("meet @ noon") is None
    assert patterns.EMAIL.search("user@localhost") is None  # no TLD dot


# --- contextual ID types ----------------------------------------------------


def test_drivers_license_contextual():
    _assert_value(
        patterns.DRIVERS_LICENSE,
        "Identity was verified against driver's license C4821736.",
        "C4821736",
    )
    _assert_value(
        patterns.DRIVERS_LICENSE,
        "The license number C4821736 was photocopied for the I-9 file.",
        "C4821736",
    )
    _assert_value(patterns.DRIVERS_LICENSE, "Driver's License | C4821736", "C4821736")


def test_drivers_license_requires_marker_and_shape():
    assert patterns.DRIVERS_LICENSE.search("the code C4821736 was found") is None  # no marker
    assert patterns.DRIVERS_LICENSE.search("driver's license ABC1234") is None  # wrong shape
    assert patterns.DRIVERS_LICENSE.search("driver's license C48217361") is None  # 8 digits


def test_passport_contextual():
    _assert_value(
        patterns.PASSPORT, "Passport number 731945082 was provided.", "731945082"
    )
    _assert_value(
        patterns.PASSPORT, "The identity document on file is passport 731945082.", "731945082"
    )
    _assert_value(patterns.PASSPORT, "Passport | 731945082", "731945082")
    assert patterns.PASSPORT.search("the number 731945082 appears") is None
    assert patterns.PASSPORT.search("passport 7319450") is None  # 7 digits


def test_financial_account_contextual():
    _assert_value(
        patterns.FINANCIAL_ACCOUNT, "Direct deposit is routed to account 84739201855.", "84739201855"
    )
    _assert_value(
        patterns.FINANCIAL_ACCOUNT,
        "The reimbursement was issued to account number 8473920185.",
        "8473920185",
    )
    _assert_value(patterns.FINANCIAL_ACCOUNT, "Account | 84739201855", "84739201855")
    assert patterns.FINANCIAL_ACCOUNT.search("the number 84739201855 appears") is None


def test_employee_id_shape_based():
    # Shape-based deliberately: cred-dump rows sit far below the header
    # line, so context cannot be required (patterns.py rationale).
    _assert_value(patterns.EMPLOYEE_ID, "filed under employee ID E48213.", "E48213")
    _assert_value(patterns.EMPLOYEE_ID, "E48213 | malvarez42 | Maple33! | 523-41-8722", "E48213")
    assert patterns.EMPLOYEE_ID.search("code E482131 here") is None  # 6 digits
    assert patterns.EMPLOYEE_ID.search("code XE48213 here") is None  # letter run continues left
    assert patterns.EMPLOYEE_ID.search("code e48213 here") is None  # lowercase is not the format
