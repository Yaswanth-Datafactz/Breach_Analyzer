"""Tier-0 deterministic detection (docs/plan.md §3: "TIER 0 deterministic
detectors (regex + checksums ...) -- free"). `run_tier0` is the interface
services/pipeline.py consumes: pure text in, `DetectedElement` list out,
no I/O, no model calls, $0.

The three submodules split by testability concern:

- patterns.py   -- WHERE a value shape can occur (compiled regex, real
                   offsets for free);
- validators.py -- WHETHER the value passes its computable check (Luhn,
                   SSN structural rules, NANP, email syntax) -> the
                   validation_status;
- context.py    -- whether the SURROUNDINGS mark it as a trap (order
                   numbers, placeholders, TEST records, staff signature
                   blocks) -> the trap_reason downgrade.

Detectors run in a fixed precedence order (most self-evident shape
first) and claim character spans as they go -- a later pattern can never
re-type text a stronger detector already claimed, which is how "SSN vs
9-digit passport" and "card vs digit-run" ambiguities are resolved
deterministically.

Everything here maps 1:1 onto `pii_elements` columns (plan §4):
element_type / value_raw / value_normalized / char_start / char_end /
validation_status, with detector='regex' or 'checksum' and trap_reason
destined for the `signals` JSONB -- the pipeline owns that persistence.
Note `employee_id` hits: not a `pii_elements` category (not in the §4
enum) but the PartialIdentifiers ER join key, so the pipeline routes
them into mention/ER features rather than element rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.detectors import context, patterns, validators

__all__ = ["DetectedElement", "run_tier0"]

_DIGITS_RE = re.compile(r"\D")

# (element_type, compiled pattern) in precedence order. Order is
# load-bearing: email first (letters + @ are unambiguous), then card
# before phone before SSN (longest digit structures claim first), then
# the contextual marker+value patterns, then employee_id.
_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", patterns.EMAIL),
    ("credit_card", patterns.CARD_GROUPED_16),
    ("credit_card", patterns.CARD_GROUPED_15),
    ("credit_card", patterns.CARD_CONTIGUOUS),
    ("phone", patterns.PHONE_PAREN),
    ("phone", patterns.PHONE_SEPARATED),
    ("ssn", patterns.SSN_DASHED),
    ("ssn", patterns.SSN_SPACED),
    ("ssn_last4", patterns.SSN_LAST4),
    ("passport", patterns.PASSPORT),
    ("ssn", patterns.SSN_UNDASHED),
    ("financial_account", patterns.FINANCIAL_ACCOUNT),
    ("drivers_license", patterns.DRIVERS_LICENSE),
    ("employee_id", patterns.EMPLOYEE_ID),
)


@dataclass(frozen=True)
class DetectedElement:
    """One tier-0 hit. char_start/char_end are REAL offsets into the
    passage text as given (regex start/end of the value group -- D9:
    deterministic offsets, never model-reported). trap_reason is set
    exactly when a context heuristic downgraded the hit; such hits are
    signals for tier-1 disagreement and the trap scorecard, never
    element assertions on their own."""

    element_type: str
    value_raw: str
    value_normalized: str
    char_start: int
    char_end: int
    validation_status: str
    trap_reason: str | None = None


def _normalize(element_type: str, value_raw: str) -> str:
    """Per-type normalization for the ER blocking index (plan §4
    (element_type, value_normalized)): digits-only for numeric types,
    lowercase for email, uppercase-as-matched for letter-bearing IDs."""
    if element_type in ("ssn", "ssn_last4", "credit_card", "passport", "financial_account"):
        return _DIGITS_RE.sub("", value_raw)
    if element_type == "phone":
        digits = _DIGITS_RE.sub("", value_raw)
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]  # strip NANP country code
        return digits
    if element_type == "email":
        return value_raw.lower()
    # drivers_license ([A-Z]\d{7}) and employee_id (E\d{5}) match
    # uppercase already; keep them verbatim.
    return value_raw


def _overlaps(start: int, end: int, claimed: list[tuple[int, int]]) -> bool:
    return any(start < c_end and end > c_start for c_start, c_end in claimed)


def run_tier0(passage_text: str) -> list[DetectedElement]:
    """Run every deterministic detector over one passage's text.

    Returns hits sorted by char_start. Placeholder-shaped values
    ("{{ssn}}", "XXX-XX-1234", "[SSN]") never match the digit patterns
    in the first place; a syntactically real value in trap context comes
    back downgraded (validation_status='format_only' + trap_reason)
    rather than dropped, so nothing tier 0 saw is silently lost.
    """
    claimed: list[tuple[int, int]] = []
    results: list[DetectedElement] = []
    for element_type, pattern in _DETECTORS:
        for match in pattern.finditer(passage_text):
            start, end = match.start(1), match.end(1)
            if _overlaps(start, end, claimed):
                continue
            value_raw = match.group(1)
            if context.is_placeholder_value(value_raw):
                continue  # defense in depth; digit patterns can't match these
            value_normalized = _normalize(element_type, value_raw)
            trap_reason = context.trap_reason_for(passage_text, start, element_type)
            if trap_reason is not None:
                validation_status = validators.FORMAT_ONLY
            else:
                validation_status = validators.status_for(element_type, value_normalized)
            claimed.append((start, end))
            results.append(
                DetectedElement(
                    element_type=element_type,
                    value_raw=value_raw,
                    value_normalized=value_normalized,
                    char_start=start,
                    char_end=end,
                    validation_status=validation_status,
                    trap_reason=trap_reason,
                )
            )
    return sorted(results, key=lambda e: (e.char_start, e.char_end))
