"""Trap-context heuristics (docs/plan.md §8 FalsePositiveTraps, §10 trap
scorecard). Pure functions over the passage text and a hit's span --
no I/O, no state -- so every heuristic is unit-testable in isolation
(test_detector_context.py) and against full trap documents
(test_detector_tier0.py).

A trap hit is DOWNGRADED, never dropped: run_tier0 keeps it with
validation_status='format_only' and a trap_reason, so the trap remains
visible as a signal (tier-1 disagreement checks, the accuracy trap
scorecard) without ever becoming an element assertion.

The corpus trap kinds these must catch (corpusgen/scenarios.py
TRAP_KINDS), and which heuristic owns each:

- trap_order_number    -> business_reference (SSN-shaped value beside
                          "Order #")
- trap_test_ssn        -> test_record ("TEST RECORD -- DO NOT PROCESS",
                          "SAMPLE -- Test User" rows above the value)
- trap_placeholder     -> placeholder ("{{ssn}}", "XXX-XX-1234" -- these
                          never match a digit pattern at all, plus
                          format-example markers for values that do)
- trap_signature_email/
  trap_signature_phone -> signature_block ("Reported by:" staff blocks)
- trap_card_invalid    -> NOT a context trap: the Luhn check itself is
                          the signal (validators.py -> invalid_checksum)

Nearest-marker-wins: a business-reference marker only downgrades a hit
when no PII-affirming label sits CLOSER to the value. The medical-claim
table is the live case: "Claim # | CLM-..." a few rows above
"Member SSN | 123-45-6789" must not trap the SSN -- "Member SSN" is the
nearest label and it affirms.
"""

from __future__ import annotations

import re

# trap_reason values (persisted into pii_elements.signals by the
# pipeline; string constants so tests and the accuracy scorer share them).
TRAP_BUSINESS_REFERENCE = "business_reference"
TRAP_PLACEHOLDER = "placeholder"
TRAP_TEST_RECORD = "test_record"
TRAP_SIGNATURE_BLOCK = "signature_block"
# Applied by the PIPELINE, not by a text heuristic here: email/phone hits
# inside an eml {'part': 'headers'} passage are the document HANDLER's
# routing addresses (corpusgen records From/To exclusively as
# trap_staff_email plantings; subject PII is never planted in headers).
# The pure-text heuristics cannot know a passage is an RFC-5322 header
# block -- the pipeline can, from the passage locator (services/pipeline.py
# _cross_passage_trap_reason).
TRAP_STAFF_HEADER = "staff_header"

# Window sizes (chars of passage text before the hit). Sized against the
# corpus layouts: a labeled table line puts its marker within ~30 chars;
# the test-ticket marker rows sit 2-3 short lines (~120 chars) above the
# value; a staff signature block (name + title/company + email lines) puts
# "Reported by:" ~110 chars before the phone.
BUSINESS_REF_WINDOW = 64
PLACEHOLDER_WINDOW = 80
TEST_RECORD_WINDOW = 240
SIGNATURE_WINDOW = 160

# Business reference labels ("Order #", "Invoice Number", "PO #", "ticket
# TCK-...", "Case No."). `claim` is included: an SSN-shaped value directly
# labeled "Claim #" is a reference number, while "Member SSN" wins by
# proximity when both appear (nearest-marker rule).
_BUSINESS_REF_RE = re.compile(
    r"(?i)\b(?:order|invoice|ticket|case|claim|p\.?o\.?|purchase\s+order|"
    r"ref(?:erence)?|confirmation|tracking|serial)\b"
    r"(?:\s+(?:number|no\.?|num))?\s*[:#]?"
)

# Labels that AFFIRM a value as personal data -- the counterweight in the
# nearest-marker comparison. Deliberately broad: these only ever protect a
# hit from a downgrade, never create one.
_PII_AFFIRMING_RE = re.compile(
    r"(?i)(?:\bssn\b|social\s+security|card\s+number|credit\s+card|card\s+on\s+file|"
    r"\bcard\b|\bacc(?:oun)?t\b|direct\s+deposit|payroll|w-2|passport|licen[cs]e|"
    r"\bmember\b|\bemployee\b|\bphone\b|\bcall\b|e-?mail|\bcontact\b)"
)

# Placeholder VALUES: "{{ssn}}"-style template fields, "[SSN]"-style
# bracket tokens, X-masked shapes ("XXX-XX-1234", "XXXX XXXX XXXX 1111").
# Digit patterns can't match these anyway; the check exists so run_tier0
# can reject them explicitly and tests can prove the rejection.
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^\{\{[^}]*\}\}$|^\[[^\]]+\]$|^<[^>]+>$|[Xx*#]{2,}[\s-]"
)

# Placeholder CONTEXT: format instructions around a syntactically real
# value ("in the format 000-00-0000", "e.g. 123-45-6789", "template
# field ... must be replaced").
_PLACEHOLDER_MARKER_RE = re.compile(
    r"(?i)(?:\bin\s+the\s+format|\bformat[:\s]|\btemplate\b|\bplaceholder\b|"
    r"\bfor\s+example\b|\be\.g\.|\bsuch\s+as\b|\bmust\s+be\s+replaced\b)"
)

# Test/sample record markers. Multi-word phrases match case-insensitively;
# the bare single words only count in ALL CAPS ("TEST", "SAMPLE") -- the
# record-marker convention -- so prose "sample" or the reserved
# example.com domains never trip it.
_TEST_RECORD_PHRASE_RE = re.compile(
    r"(?i)\b(?:test\s+record|do\s+not\s+process|test\s+user|test\s+data|"
    r"sample\s+data|sample\s+record|qa\s+automation|dummy)\b"
)
_TEST_RECORD_CAPS_RE = re.compile(r"\b(?:TEST|SAMPLE|DUMMY|EXAMPLE)\b")

# Signature-block markers: sign-offs and attribution lines that precede a
# staff contact block. "From:" is deliberately absent -- a memo's From:
# header sits within window range of the first body sentence, which can
# legitimately carry the SUBJECT's phone (templates.hr_memo). "thanks" is
# included because it is corpusgen's email sign-off (templates.py:
# "Thanks,\n{sender.name}\n{sender.title}, {COMPANY}") -- measured on the
# full-corpus B1 run, its absence leaked every eml body signature
# email/phone as validation_status='valid'.
_SIGNATURE_RE = re.compile(
    r"(?i)(?:\breported\s+by\b|\bhandled\s+by\b|\bregards\b|\bbest\s+regards\b|"
    r"\bsincerely\b|\byours\s+truly\b|\brespectfully\b|\bsigned\b|"
    r"\bthanks\b|\bthank\s+you\b)"
)


def preceding_window(text: str, start: int, size: int) -> str:
    """The `size` characters of passage text before offset `start`."""
    return text[max(0, start - size) : start]


def _last_match_end(pattern: re.Pattern[str], window: str) -> int | None:
    end: int | None = None
    for m in pattern.finditer(window):
        end = m.end()
    return end


def business_reference_trap(text: str, start: int, window: int = BUSINESS_REF_WINDOW) -> bool:
    """True when the nearest label before the hit marks a business
    reference number (order/invoice/ticket/PO/case/...), with no
    PII-affirming label closer to the value."""
    win = preceding_window(text, start, window)
    ref_end = _last_match_end(_BUSINESS_REF_RE, win)
    if ref_end is None:
        return False
    affirm_end = _last_match_end(_PII_AFFIRMING_RE, win)
    return affirm_end is None or affirm_end < ref_end


def is_placeholder_value(value: str) -> bool:
    return _PLACEHOLDER_VALUE_RE.search(value) is not None


def placeholder_trap(text: str, start: int, window: int = PLACEHOLDER_WINDOW) -> bool:
    """True when the hit is introduced as a format example or template
    field rather than someone's actual value."""
    return _PLACEHOLDER_MARKER_RE.search(preceding_window(text, start, window)) is not None


def is_test_record_trap(text: str, start: int, window: int = TEST_RECORD_WINDOW) -> bool:
    """True when the surrounding record is explicitly marked
    TEST/SAMPLE/DUMMY (QA fixtures, not breached persons).

    Named with the `is_` prefix (unlike its sibling heuristics) because a
    bare `test_record_trap` imported unqualified into a pytest module gets
    COLLECTED as a test and errors on its parameters."""
    win = preceding_window(text, start, window)
    return (
        _TEST_RECORD_PHRASE_RE.search(win) is not None
        or _TEST_RECORD_CAPS_RE.search(win) is not None
    )


def signature_block_trap(text: str, start: int, window: int = SIGNATURE_WINDOW) -> bool:
    """True when the hit sits under a sign-off/attribution line -- the
    document HANDLER's business contact details, not subject PII
    (templates.py: full signature blocks exist only as recorded trap
    plantings). Applied to email/phone hits only (run_tier0)."""
    return _SIGNATURE_RE.search(preceding_window(text, start, window)) is not None


def trap_reason_for(text: str, start: int, element_type: str) -> str | None:
    """First matching trap heuristic for a hit, most specific first --
    or None for a clean hit. The single entry point run_tier0 uses."""
    if element_type in ("ssn", "ssn_last4", "credit_card") and business_reference_trap(text, start):
        return TRAP_BUSINESS_REFERENCE
    if placeholder_trap(text, start):
        return TRAP_PLACEHOLDER
    if is_test_record_trap(text, start):
        return TRAP_TEST_RECORD
    if element_type in ("email", "phone") and signature_block_trap(text, start):
        return TRAP_SIGNATURE_BLOCK
    return None
