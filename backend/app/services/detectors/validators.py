"""Tier-0 structural validators (docs/plan.md §3 "regex + checksums: SSN
area/group rules, Luhn, phone/email/DL/passport patterns"). Pure
functions over normalized values -- no I/O, no state, unit-tested
directly and via the corpusgen conformance suite.

Status semantics (`pii_elements.validation_status`, plan §4):

- ``valid``            -- the value passed a real, computable check for
                          its type (SSN area/group/serial rules, Luhn,
                          NANP area+exchange rules, email syntax).
- ``invalid_checksum`` -- the value MATCHED the type's format but FAILED
                          its computable check (Luhn-invalid card,
                          SSN with a 000/666/9xx area or zero
                          group/serial). Kept as a *signal* for
                          tier-0-vs-tier-1 disagreement and trap
                          scoring, never asserted as an element on its
                          own (plan §4). SSNs have no literal checksum;
                          the structural-rule failure is recorded under
                          the same status because the enum's three
                          values partition exactly into
                          passed / failed / unverifiable.
- ``format_only``      -- the type has no computable check beyond
                          format + context (driver's license, passport,
                          financial account, employee ID, ssn_last4),
                          or a hit was downgraded by trap context
                          (context.py sets a ``trap_reason`` alongside;
                          a plain format_only has ``trap_reason=None``).
"""

from __future__ import annotations

import re

VALID = "valid"
INVALID_CHECKSUM = "invalid_checksum"
FORMAT_ONLY = "format_only"

# The element types status_for() can return a real verdict for -- for these
# `valid` is attainable and anything else means the value failed (or was
# trap-downgraded). For every OTHER type FORMAT_ONLY is the best possible
# status ("no computable check exists", this module's docstring), NOT a
# failure: consumers deciding usability (exposure evidence, ER identifier
# gates) must require `valid` only for these types and accept a plain
# format_only (trap_reason=None) for the rest.
VERDICT_TYPES = frozenset({"ssn", "credit_card", "phone", "email"})

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


def ssn_is_structurally_valid(digits: str) -> bool:
    """SSA structural rules (docs/plan.md §8): area 001-899 excluding 000
    and 666, nonzero group, nonzero serial. `digits` is the 9-digit
    normalized form. corpusgen's make_ssn generates only values this
    accepts -- the conformance test holds both sides to that contract."""
    if len(digits) != 9 or not digits.isdigit():
        return False
    area, group, serial = int(digits[:3]), int(digits[3:5]), int(digits[5:])
    if area == 0 or area == 666 or area > 899:
        return False
    return group != 0 and serial != 0


def luhn_checksum(digits: str) -> int:
    """Mod-10 checksum over a digits-only string (0 == valid). Mirrors
    corpusgen/identities.py so the generator and the detector can never
    drift on what "Luhn-valid" means."""
    values = [int(d) for d in digits]
    odd = values[-1::-2]
    even = values[-2::-2]
    return (sum(odd) + sum(sum(divmod(2 * d, 10)) for d in even)) % 10


def luhn_is_valid(digits: str) -> bool:
    return digits.isdigit() and len(digits) >= 2 and luhn_checksum(digits) == 0


def phone_is_nanp_valid(digits: str) -> bool:
    """NANP structural rules over a normalized 10-digit (or 11 with
    leading country code 1) string: area code and exchange each start
    2-9. The phone patterns already bake this in; this is the same rule
    as a pure function for the status decision and for direct tests."""
    if not digits.isdigit():
        return False
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return False
    return digits[0] != "0" and digits[0] != "1" and digits[3] not in ("0", "1")


def email_is_valid(value: str) -> bool:
    return _EMAIL_RE.match(value) is not None


def status_for(element_type: str, value_normalized: str) -> str:
    """Map a detected value to its validation_status (trap downgrades are
    applied AFTER this by run_tier0 -- a trap hit becomes format_only
    with a trap_reason regardless of what this returns)."""
    if element_type == "ssn":
        return VALID if ssn_is_structurally_valid(value_normalized) else INVALID_CHECKSUM
    if element_type == "credit_card":
        return VALID if luhn_is_valid(value_normalized) else INVALID_CHECKSUM
    if element_type == "phone":
        return VALID if phone_is_nanp_valid(value_normalized) else INVALID_CHECKSUM
    if element_type == "email":
        return VALID if email_is_valid(value_normalized) else FORMAT_ONLY
    # drivers_license / passport / financial_account / employee_id /
    # ssn_last4: nothing computable beyond format + context.
    return FORMAT_ONLY
