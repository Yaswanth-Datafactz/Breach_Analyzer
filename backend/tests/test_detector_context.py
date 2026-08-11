"""Unit tests for the trap-context heuristics as pure functions
(docs/plan.md §15 "trap contexts"). Whole-document trap behavior is in
test_detector_tier0.py; here each heuristic is exercised in isolation,
including the nearest-marker-wins rule and the deliberate non-triggers
(prose "sample", example.com domains, memo "From:" headers)."""

from app.services.detectors import context
from app.services.detectors.context import (
    TRAP_BUSINESS_REFERENCE,
    TRAP_PLACEHOLDER,
    TRAP_SIGNATURE_BLOCK,
    TRAP_TEST_RECORD,
    business_reference_trap,
    is_placeholder_value,
    is_test_record_trap,
    placeholder_trap,
    preceding_window,
    signature_block_trap,
    trap_reason_for,
)


def _start_of(text: str, value: str) -> int:
    start = text.index(value)
    assert start >= 0
    return start


# --- preceding window -------------------------------------------------------


def test_preceding_window_clamps_at_zero():
    assert preceding_window("abcdef", 3, 100) == "abc"
    assert preceding_window("abcdef", 0, 10) == ""
    assert preceding_window("abcdef", 5, 2) == "de"


# --- business reference -----------------------------------------------------


def test_order_number_marker_traps():
    text = "Order # | 523-41-8722"
    assert business_reference_trap(text, _start_of(text, "523-41-8722"))


def test_invoice_ticket_case_po_markers_trap():
    for label in ("Invoice #", "Ticket Number", "Case No.", "PO #", "Purchase Order", "Ref #"):
        text = f"{label} 523-41-8722"
        assert business_reference_trap(text, _start_of(text, "523-41-8722")), label


def test_nearest_marker_wins_affirming_label_closer():
    # The live medical-claim layout: "Claim #" rows above, "Member SSN"
    # immediately before the value -- must NOT trap.
    text = "Claim # | CLM-2025-18233\nMember SSN | 523-41-8722"
    assert not business_reference_trap(text, _start_of(text, "523-41-8722"))


def test_nearest_marker_wins_business_label_closer():
    text = "Member SSN unavailable; use Order # 523-41-8722"
    assert business_reference_trap(text, _start_of(text, "523-41-8722"))


def test_no_marker_no_trap():
    text = "Payroll currently lists the Social Security number 523-41-8722."
    assert not business_reference_trap(text, _start_of(text, "523-41-8722"))


def test_reimbursement_does_not_false_match_ref():
    # \bref\b must not fire inside "reimbursement" (the Luhn-trap memo
    # says "Rejected reimbursement payment" right above the card value).
    text = "Re: Rejected reimbursement payment\n\nThe card number 4111 1111 1111 1112 was"
    assert not business_reference_trap(text, _start_of(text, "4111 1111 1111 1112"))


# --- placeholders -----------------------------------------------------------


def test_placeholder_value_shapes():
    assert is_placeholder_value("{{ssn}}")
    assert is_placeholder_value("[SSN]")
    assert is_placeholder_value("<ssn>")
    assert is_placeholder_value("XXX-XX-1234")
    assert is_placeholder_value("XXXX XXXX XXXX 1111")
    assert not is_placeholder_value("523-41-8722")
    assert not is_placeholder_value("maria.alvarez12@example.net")


def test_placeholder_context_markers():
    text = "enter the Social Security number in the format 000-00-0000."
    assert placeholder_trap(text, _start_of(text, "000-00-0000"))
    text2 = "an SSN, e.g. 523-41-8722, must be redacted"
    assert placeholder_trap(text2, _start_of(text2, "523-41-8722"))
    text3 = "Payroll lists the Social Security number 523-41-8722."
    assert not placeholder_trap(text3, _start_of(text3, "523-41-8722"))


# --- TEST/SAMPLE records ----------------------------------------------------


def test_test_record_markers_trap():
    text = "Status | TEST RECORD — DO NOT PROCESS\nCustomer | SAMPLE — Test User\nSSN | 523-41-8722"
    assert is_test_record_trap(text, _start_of(text, "523-41-8722"))


def test_bare_caps_test_sample_trap():
    for marker in ("TEST", "SAMPLE", "DUMMY"):
        text = f"{marker} entry: 523-41-8722"
        assert is_test_record_trap(text, _start_of(text, "523-41-8722")), marker


def test_lowercase_prose_and_example_domains_do_not_trap():
    # "sample"/"test" as ordinary words and the reserved example.com
    # domains must never trip the record marker.
    text = "we sampled the latest data; contact maria.alvarez12@example.net for 523-41-8722"
    assert not is_test_record_trap(text, _start_of(text, "523-41-8722"))


def test_do_not_distribute_banner_does_not_trap():
    # The cred-dump banner: "DO NOT DISTRIBUTE" is a handling caveat on
    # REAL data, not a test-record marker.
    text = "# meridian internal extract — DO NOT DISTRIBUTE\n# pulled from hr-auth replica\nE48213 | 523-41-8722"
    assert not is_test_record_trap(text, _start_of(text, "523-41-8722"))


# --- signature blocks -------------------------------------------------------


def test_signature_attribution_lines_trap():
    text = (
        "Reported by: Dana Whitfield\nHR Business Partner, Meridian Benefits Group\n"
        "dana.whitfield@meridianbenefits.example"
    )
    assert signature_block_trap(text, _start_of(text, "dana.whitfield@meridianbenefits.example"))


def test_sign_off_words_trap():
    for sign_off in ("Regards,", "Sincerely,", "Best regards,"):
        text = f"{sign_off}\nDana Whitfield\n(312) 555-0163"
        assert signature_block_trap(text, _start_of(text, "(312) 555-0163")), sign_off


def test_memo_from_header_does_not_trap_subject_phone():
    # hr_memo shape: the From: header sits within window range of the
    # first body sentence, which may carry the SUBJECT's phone -- "From:"
    # is deliberately not a signature marker (context.py rationale).
    text = (
        "To: Maria Alvarez\nFrom: Dana Whitfield, Payroll Specialist\n"
        "Date: 2025-06-01\nRe: Direct deposit verification\n\n"
        "The daytime contact number on file is (415) 555-0137."
    )
    assert not signature_block_trap(text, _start_of(text, "(415) 555-0137"))


# --- dispatch ---------------------------------------------------------------


def test_trap_reason_for_selects_most_specific_first():
    order_text = "Order # | 523-41-8722"
    assert trap_reason_for(order_text, _start_of(order_text, "523-41-8722"), "ssn") == (
        TRAP_BUSINESS_REFERENCE
    )
    fmt_text = "in the format 000-00-0000"
    assert trap_reason_for(fmt_text, _start_of(fmt_text, "000-00-0000"), "ssn") == TRAP_PLACEHOLDER
    test_text = "TEST RECORD\nSSN | 523-41-8722"
    assert trap_reason_for(test_text, _start_of(test_text, "523-41-8722"), "ssn") == (
        TRAP_TEST_RECORD
    )
    sig_text = "Regards,\nDana\n(312) 555-0163"
    assert trap_reason_for(sig_text, _start_of(sig_text, "(312) 555-0163"), "phone") == (
        TRAP_SIGNATURE_BLOCK
    )


def test_trap_reason_for_is_type_scoped():
    # Business-ref applies to ssn/card shapes only; signature applies to
    # email/phone only.
    text = "Order # | E48213"
    assert trap_reason_for(text, _start_of(text, "E48213"), "employee_id") is None
    sig = "Regards,\nDana\n523-41-8722"
    assert trap_reason_for(sig, _start_of(sig, "523-41-8722"), "ssn") is None


def test_trap_reason_for_clean_hit_is_none():
    text = "Payroll currently lists the Social Security number 523-41-8722 for Maria Alvarez."
    assert trap_reason_for(text, _start_of(text, "523-41-8722"), "ssn") is None
    assert context.trap_reason_for(text, _start_of(text, "523-41-8722"), "credit_card") is None
