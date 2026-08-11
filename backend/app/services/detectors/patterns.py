"""Compiled tier-0 regex library (docs/plan.md §3 "TIER 0 deterministic
detectors", §8 corpus formats). Every pattern here exists because
corpusgen generates that exact shape -- the library is written AGAINST
corpusgen/identities.py + templates.py, and
tests/test_detector_corpusgen_conformance.py proves every generated
format round-trips through a detector. Formats the corpus never emits
(7-digit local phones, IBAN, non-US ID shapes) are deliberately absent:
tier 0 is a precision instrument, and anything it cannot claim with a
real validator or explicit context falls through to tier 1.

Two pattern families:

- *Shape-based* patterns (SSN dashed/spaced, credit card, phone, email,
  employee ID): the value's format alone is distinctive enough to
  detect anywhere. Group 1 is always the value.
- *Contextual* patterns (ssn_last4, undashed SSN, passport, driver's
  license, financial account): the raw shape (4/9/10-11 bare digits, one
  letter + 7 digits) is too generic to assert without a label, so the
  pattern matches marker + value in one regex -- exactly how
  templates.py prints them ("Passport number {v}", "account {v}",
  "driver's license {v}", "ending in {v}", table rows "Label | value").
  Group 1 is still the value; the marker chars are never part of the
  reported span.

All digit patterns carry `(?<![\\d-])` / `(?![\\d-])` guards so a hit can
never start or end inside a longer digit/dash run -- the guard that stops
"123-45-6789 4111 1111 1111 1111" from yielding a phantom 13-digit card
built from the SSN's tail (regression-tested in test_detector_tier0.py).

Offsets come free and REAL from `re` (D9: tier-0 offsets were never the
problem -- model-reported ones were), so `m.start(1)`/`m.end(1)` are the
char_start/char_end that go straight into `pii_elements`.
"""

from __future__ import annotations

import re

# --- SSN (corpusgen make_ssn: always dashed ddd-dd-dddd; undashed and
# --- spaced accepted for robustness against re-typed values) ---------------

# Dashed 3-2-4. The 3-2-4 dash shape cannot align inside a NANP 3-3-4
# phone or a 4-4-4-4 card, so this is safe shape-based.
SSN_DASHED = re.compile(r"(?<![\d-])(\d{3}-\d{2}-\d{4})(?![\d-])")

# Spaced 3 2 4. Both lookarounds also reject a digit one space away so a
# spaced SSN can't be carved out of a longer spaced digit series.
SSN_SPACED = re.compile(r"(?<!\d )(?<!\d)(\d{3} \d{2} \d{4})(?!\d)(?! \d)")

# Undashed: 9 bare digits are also a passport shape (corpusgen passports
# are exactly 9 digits), so an SSN label must appear first. The gap
# between marker and value is deliberately TIGHT (separator chars only,
# matching how templates.py prints labels: "SSN {v}", "SSN: {v}",
# "Member SSN | {v}") -- a permissive gap would let one marker capture a
# value that actually belongs to a different, closer label ("SSN and
# passport 123456789" must type as passport, not ssn).
SSN_UNDASHED = re.compile(
    r"(?i:social\s+security(?:\s+number)?|\bssn\b)\s*[:#|]?\s*(?<![\d-])(\d{9})(?![\d-])"
)

# Last-4 reference: "ending in 1234", "SSN ending 1234", "SSN (last 4) |
# 1234" (templates.py _SENTENCES["ssn_last4"] + the _CLAIM_LABELS table
# label). The SSN marker and the last-4 marker must BOTH appear.
SSN_LAST4 = re.compile(
    r"(?i:social\s+security(?:\s+number)?|\bssn\b)[^\n]{0,40}?"
    r"(?i:ending\s+in|ending|last[\s-]?(?:four|4))"
    r"[^\n\d]{0,15}(?<!\d)(\d{4})(?![\d-])"
)

# --- credit card (corpusgen make_credit_card: 16 digits spaced 4-4-4-4,
# --- Visa/MC/Discover prefixes; 13-19 digit candidates per the task) -------

# Uniform-separator grouped forms: 4-4-4-4 (16) and 4-6-5 (15, Amex
# grouping). The backreference forces one consistent separator -- mixed
# "4111 1111-1111 1111" is not a card presentation.
CARD_GROUPED_16 = re.compile(r"(?<![\d-])(\d{4}([ -])\d{4}\2\d{4}\2\d{4})(?![\d-])")
CARD_GROUPED_15 = re.compile(r"(?<![\d-])(\d{4}([ -])\d{6}\2\d{5})(?![\d-])")

# Contiguous 13-19 digits (PAN length range). Luhn (validators.py)
# decides valid vs invalid_checksum; context.py decides trap vs not.
CARD_CONTIGUOUS = re.compile(r"(?<![\d-])(\d{13,19})(?![\d-])")

# --- phone (corpusgen make_phone / templates make_staff: "(ddd) 555-01dd";
# --- NANP area+exchange rules baked in: first digit of each is 2-9) --------

PHONE_PAREN = re.compile(r"(?<![\d-])(\(([2-9]\d{2})\)\s?[2-9]\d{2}-\d{4})(?![\d-])")
# Separated 3-3-4 with one consistent separator (backreference), optional
# +1/1 country prefix.
PHONE_SEPARATED = re.compile(
    r"(?<![\d-])((?:\+?1[ .-])?[2-9]\d{2}([ .-])[2-9]\d{2}\2\d{4})(?![\d-])"
)

# --- email (corpusgen: person@example.com|net|org, staff
# --- first.last@meridianbenefits.example) ----------------------------------

EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")

# --- driver's license (corpusgen: one letter from ABCDEFGHJKLMNPRSTUVWXYZ +
# --- 7 digits; detector accepts any A-Z -- broader is safe under context) --

DRIVERS_LICENSE = re.compile(
    r"(?i:driver'?s?\s+licen[cs]e|licen[cs]e(?:\s+(?:number|no\.?))?|\bdl\b)"
    r"\s*[:#|]?\s*([A-Z]\d{7})(?![0-9A-Za-z])"
)

# --- passport (corpusgen: 9 digits, contextual "passport [number] {v}") ----

PASSPORT = re.compile(
    r"(?i:passport)(?:(?i:\s+(?:number|no\.?)))?\s*[:#|]?\s*(?<![\d-])(\d{9})(?![\d-])"
)

# --- financial account (corpusgen: 10-11 digits; contextual
# --- "account [number] {v}" / "Account | {v}"; 9-12 accepted) --------------

FINANCIAL_ACCOUNT = re.compile(
    r"(?i:acc(?:oun)?t)(?:(?i:\s+(?:number|no\.?)))?\s*[:#|]?\s*(?<![\d-])(\d{9,12})(?![\d-])"
)

# --- employee ID (corpusgen: E + 5 digits, e.g. E48213). Shape-based:
# --- cred-dump rows sit far below their header line, so context can't be
# --- required; the E\d{5} shape with hard boundaries is distinctive. -------

EMPLOYEE_ID = re.compile(r"(?<![A-Za-z0-9])(E\d{5})(?![A-Za-z0-9])")
