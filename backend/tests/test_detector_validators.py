"""Unit tests for tier-0 structural validators (docs/plan.md §15:
"detectors (valid/invalid SSN, Luhn, trap contexts)"). Pure-function
tests; the corpusgen conformance suite additionally proves these accept
every value the generator plants."""

import pytest

from app.services.detectors import validators
from app.services.detectors.validators import (
    FORMAT_ONLY,
    INVALID_CHECKSUM,
    VALID,
    email_is_valid,
    luhn_checksum,
    luhn_is_valid,
    phone_is_nanp_valid,
    ssn_is_structurally_valid,
    status_for,
)

# --- SSN structural rules (area 001-899 excl 000/666, nonzero group/serial)


@pytest.mark.parametrize(
    "digits",
    [
        "001010001",  # lowest legal area/group/serial
        "899999999",  # highest legal area
        "523418722",
        "665121234",  # 665 is legal; only exactly 666 is excluded
        "667121234",
    ],
)
def test_ssn_valid_structures(digits):
    assert ssn_is_structurally_valid(digits)


@pytest.mark.parametrize(
    ("digits", "why"),
    [
        ("000121234", "area 000"),
        ("666121234", "area 666"),
        ("900121234", "area 900"),
        ("999121234", "area 999"),
        ("123001234", "group 00"),
        ("123450000", "serial 0000"),
        ("12345678", "8 digits"),
        ("1234567890", "10 digits"),
        ("12345678a", "non-digit"),
        ("", "empty"),
    ],
)
def test_ssn_invalid_structures(digits, why):
    assert not ssn_is_structurally_valid(digits), why


# --- Luhn -------------------------------------------------------------------


@pytest.mark.parametrize(
    "digits",
    [
        "79927398713",  # the classic worked example
        "4111111111111111",
        "4222222222222",  # 13-digit
        "378282246310005",  # 15-digit, 4-6-5 grouping family
        "6011111111111117",
    ],
)
def test_luhn_valid(digits):
    assert luhn_checksum(digits) == 0
    assert luhn_is_valid(digits)


@pytest.mark.parametrize("digits", ["79927398710", "4111111111111112", "1234567890123456"])
def test_luhn_invalid(digits):
    assert not luhn_is_valid(digits)


def test_luhn_rejects_non_digits_and_trivial_input():
    assert not luhn_is_valid("4111 1111 1111 1111")  # caller must normalize first
    assert not luhn_is_valid("")
    assert not luhn_is_valid("0")


def test_luhn_check_digit_bump_always_invalidates():
    # corpusgen's make_luhn_invalid_card corrupts the check digit by a
    # nonzero bump -- any such bump must fail here (mod-10 is exact).
    valid = "4111111111111111"
    for bump in range(1, 10):
        corrupted = valid[:-1] + str((int(valid[-1]) + bump) % 10)
        assert not luhn_is_valid(corrupted)


# --- NANP phone rules -------------------------------------------------------


@pytest.mark.parametrize("digits", ["3125550142", "2015550100", "9795550199", "13125550142"])
def test_nanp_valid(digits):
    assert phone_is_nanp_valid(digits)


@pytest.mark.parametrize(
    ("digits", "why"),
    [
        ("0125550142", "area starts 0"),
        ("1125550142", "area starts 1"),
        ("3120550142", "exchange starts 0"),
        ("3121550142", "exchange starts 1"),
        ("312555014", "9 digits"),
        ("23125550142", "11 digits without leading 1"),
        ("312555014a", "non-digit"),
    ],
)
def test_nanp_invalid(digits, why):
    assert not phone_is_nanp_valid(digits), why


# --- email ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["maria.alvarez12@example.net", "a@b.co", "first.last@meridianbenefits.example"],
)
def test_email_valid(value):
    assert email_is_valid(value)


@pytest.mark.parametrize("value", ["not-an-email", "a@b", "@example.com", "a b@example.com"])
def test_email_invalid(value):
    assert not email_is_valid(value)


# --- status dispatch --------------------------------------------------------


@pytest.mark.parametrize(
    ("element_type", "normalized", "expected"),
    [
        ("ssn", "523418722", VALID),
        ("ssn", "000121234", INVALID_CHECKSUM),
        ("credit_card", "4111111111111111", VALID),
        ("credit_card", "4111111111111112", INVALID_CHECKSUM),
        ("phone", "3125550142", VALID),
        ("phone", "1125550142", INVALID_CHECKSUM),
        ("email", "maria.alvarez12@example.net", VALID),
        # No computable check beyond format + context:
        ("ssn_last4", "8722", FORMAT_ONLY),
        ("drivers_license", "C4821736", FORMAT_ONLY),
        ("passport", "731945082", FORMAT_ONLY),
        ("financial_account", "84739201855", FORMAT_ONLY),
        ("employee_id", "E48213", FORMAT_ONLY),
    ],
)
def test_status_for(element_type, normalized, expected):
    assert status_for(element_type, normalized) == expected


def test_status_constants_match_db_check_constraint():
    # db/models.py valid_validation_status CHECK lists exactly these.
    assert {VALID, INVALID_CHECKSUM, FORMAT_ONLY} == {
        "valid",
        "invalid_checksum",
        "format_only",
    }
    assert validators.status_for("ssn", "523418722") in ("valid", "invalid_checksum", "format_only")
