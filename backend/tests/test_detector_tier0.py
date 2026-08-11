"""End-to-end tests for run_tier0 against passage texts modeled 1:1 on
the corpusgen templates (templates.py archetypes and the five
FalsePositiveTraps kinds from scenarios.py TRAP_KINDS). Every assertion
on offsets checks the REAL passage slice -- offsets are the evidence
anchor pii_elements/flag_evidence resolve to (docs/plan.md §1)."""

from app.services.detectors import DetectedElement, run_tier0

SSN = "523-41-8722"
CARD_VALID = "4111 1111 1111 1111"
CARD_INVALID = "4111 1111 1111 1112"  # Luhn check digit corrupted


def _by_type(elements: list[DetectedElement], element_type: str) -> list[DetectedElement]:
    return [e for e in elements if e.element_type == element_type]


def _assert_anchored(text: str, element: DetectedElement) -> None:
    assert text[element.char_start : element.char_end] == element.value_raw


# --- clean extractions with exact offsets -----------------------------------


def test_valid_ssn_prose_sentence():
    text = f"Payroll currently lists the Social Security number {SSN} for Maria Alvarez."
    (element,) = run_tier0(text)
    assert element.element_type == "ssn"
    assert element.value_raw == SSN
    assert element.value_normalized == "523418722"
    assert (element.char_start, element.char_end) == (text.index(SSN), text.index(SSN) + len(SSN))
    _assert_anchored(text, element)
    assert element.validation_status == "valid"
    assert element.trap_reason is None


def test_valid_card_prose_sentence():
    text = f"The corporate card on file, {CARD_VALID}, was charged for the copay."
    (element,) = run_tier0(text)
    assert element.element_type == "credit_card"
    assert element.value_normalized == "4111111111111111"
    assert element.validation_status == "valid"
    assert element.trap_reason is None
    _assert_anchored(text, element)


def test_valid_phone_and_email_sentences():
    text = (
        "Please call Maria Alvarez at (415) 555-0137 to confirm the change. "
        "Confirmation was sent to maria.alvarez12@example.net."
    )
    elements = run_tier0(text)
    (phone,) = _by_type(elements, "phone")
    (email,) = _by_type(elements, "email")
    assert phone.value_normalized == "4155550137"
    assert phone.validation_status == "valid"
    assert phone.trap_reason is None
    assert email.value_normalized == "maria.alvarez12@example.net"
    assert email.validation_status == "valid"
    for element in (phone, email):
        _assert_anchored(text, element)


def test_ssn_last4_contextual_sentences():
    for text in (
        "For verification we matched the Social Security number on file ending in 8722.",
        "The SSN ending 8722 was used to confirm the account holder.",
        "SSN (last 4) | 8722",
    ):
        (element,) = run_tier0(text)
        assert element.element_type == "ssn_last4", text
        assert element.value_raw == "8722"
        assert element.value_normalized == "8722"
        assert element.validation_status == "format_only"
        assert element.trap_reason is None
        _assert_anchored(text, element)


def test_contextual_id_types_from_claim_table_rows():
    # medical_claim / support_ticket rows render as "Label | value".
    text = (
        "Claim | Field | Value\n"
        "Member SSN | 523-41-8722\n"
        "Driver's License | C4821736\n"
        "Passport | 731945082\n"
        "Account | 84739201855\n"
        "Card on File | 4111 1111 1111 1111\n"
        "Phone | (415) 555-0137\n"
        "Email | maria.alvarez12@example.net"
    )
    elements = run_tier0(text)
    found = {e.element_type: e for e in elements}
    assert set(found) == {
        "ssn", "drivers_license", "passport", "financial_account",
        "credit_card", "phone", "email",
    }
    for element in elements:
        _assert_anchored(text, element)
        assert element.trap_reason is None
    assert found["ssn"].validation_status == "valid"
    assert found["credit_card"].validation_status == "valid"
    assert found["drivers_license"].validation_status == "format_only"
    assert found["passport"].value_normalized == "731945082"
    assert found["financial_account"].value_normalized == "84739201855"


def test_cred_dump_rows_yield_employee_id_and_ssn():
    # cred_dump renders name-free pipe rows (PartialIdentifiers): tier 0
    # must produce the employee_id join key and the SSN; username and
    # password are tier-1 semantic types, not tier-0 shapes.
    text = (
        "employee_id | username | password | ssn\n"
        "E48213 | malvarez42 | Maple33! | 523-41-8722\n"
        "E93105 | rchen07 | Cobalt71@ | 601-88-4415"
    )
    elements = run_tier0(text)
    assert [e.value_raw for e in _by_type(elements, "employee_id")] == ["E48213", "E93105"]
    ssns = _by_type(elements, "ssn")
    assert [e.value_raw for e in ssns] == ["523-41-8722", "601-88-4415"]
    assert all(e.validation_status == "valid" for e in ssns)
    assert {e.element_type for e in elements} == {"employee_id", "ssn"}
    for element in elements:
        _assert_anchored(text, element)


def test_results_sorted_by_char_start_and_spans_disjoint():
    text = (
        f"To: Maria Alvarez\nSSN {SSN} and card {CARD_VALID} on file; "
        "call (415) 555-0137 or write maria.alvarez12@example.net."
    )
    elements = run_tier0(text)
    starts = [e.char_start for e in elements]
    assert starts == sorted(starts)
    for left, right in zip(elements, elements[1:]):
        assert left.char_end <= right.char_start  # claimed spans never overlap
    for element in elements:
        _assert_anchored(text, element)


def test_ssn_directly_before_card_regression():
    # Lookbehind guard regression (patterns.py): the SSN tail must not
    # seed a phantom grouped card, and both values must type correctly.
    text = f"{SSN} {CARD_VALID}"
    elements = run_tier0(text)
    assert [(e.element_type, e.value_raw) for e in elements] == [
        ("ssn", SSN),
        ("credit_card", CARD_VALID),
    ]
    assert all(e.validation_status == "valid" for e in elements)


def test_clean_passage_yields_empty_list():
    text = (
        "1. Summary\nScheduled maintenance of the claims intake queue completed on "
        "2025-11-04. No member records were modified. Invoice INV-2025-18233 and "
        "ticket TCK-482913 were closed under order ORD-553281."
    )
    assert run_tier0(text) == []


# --- trap kind: trap_order_number (SSN-shaped order number, xlsx invoice) ---


def test_order_number_ssn_shape_is_format_only_with_trap_reason():
    text = (
        "Invoice | Field | Value\n"
        "Invoice # | INV-2025-18233\n"
        "Invoice Date | 2025-03-14\n"
        f"Order # | {SSN}\n"
        "Bill To | Acme Industrial Supply\n"
        "Payment Terms | Net 30"
    )
    (element,) = run_tier0(text)
    assert element.element_type == "ssn"
    assert element.value_raw == SSN
    assert element.validation_status == "format_only"
    assert element.trap_reason == "business_reference"
    _assert_anchored(text, element)


def test_member_ssn_near_claim_number_is_not_trapped():
    # Nearest-marker-wins: a REAL SSN in a claim table must survive the
    # "Claim #" rows above it.
    text = (
        "Claim | Field | Value\n"
        "Claim # | CLM-2025-18233\n"
        "Date of Service | 2025-03-14\n"
        f"Member SSN | {SSN}\n"
        "Status | Approved"
    )
    (element,) = run_tier0(text)
    assert element.validation_status == "valid"
    assert element.trap_reason is None


# --- trap kind: trap_card_invalid (Luhn-invalid card-like, pdf memo) --------


def test_luhn_invalid_card_is_invalid_checksum_not_trap():
    # The check digit IS the signal here -- no context downgrade, the
    # element is kept as invalid_checksum (plan §4: a signal, never an
    # element assertion).
    text = (
        "To: Accounts Payable\nFrom: Dana Whitfield, Payroll Specialist\n"
        "Date: 2025-03-14\nRe: Rejected reimbursement payment\n\n"
        f"The card number {CARD_INVALID} submitted with the reimbursement request "
        "failed validation (invalid check digit) and was not charged."
    )
    (element,) = run_tier0(text)
    assert element.element_type == "credit_card"
    assert element.validation_status == "invalid_checksum"
    assert element.trap_reason is None
    _assert_anchored(text, element)


# --- trap kind: trap_test_ssn (TEST/SAMPLE record, html ticket) -------------


def test_test_sample_record_ssn_is_flagged():
    text = (
        "Ticket | Field | Value\n"
        "Ticket # | TCK-482913\n"
        "Opened | 2025-03-14\n"
        "Status | TEST RECORD — DO NOT PROCESS\n"
        "Customer | SAMPLE — Test User\n"
        f"Member SSN | {SSN}\n\n"
        "This ticket was created by the QA automation suite to verify the export "
        "pipeline. All values are sample data."
    )
    (element,) = run_tier0(text)
    assert element.element_type == "ssn"
    assert element.validation_status == "format_only"
    assert element.trap_reason == "test_record"


# --- trap kind: trap_placeholder ({{ssn}} / XXX-XX-1234, txt memo) ----------


def test_placeholder_memo_yields_no_elements():
    text = (
        "To: Enrollment team\nFrom: Dana Whitfield, Enrollment Advisor\n"
        "Date: 2025-03-14\nRe: Letter template usage\n\n"
        "When completing the enrollment letter, enter the Social Security number "
        "in the format XXX-XX-1234.\n\n"
        "The template field {{ssn}} must be replaced with the member's actual value "
        "before mailing; letters containing the raw placeholder must not be sent."
    )
    assert run_tier0(text) == []


def test_format_example_with_real_shape_is_downgraded_to_placeholder():
    # A syntactically real-looking value in format-example context: kept,
    # but never asserted (and 000 area would fail validation anyway --
    # the trap check deliberately runs first).
    text = "enter the Social Security number in the format 000-00-0000 on the form"
    (element,) = run_tier0(text)
    assert element.element_type == "ssn"
    assert element.validation_status == "format_only"
    assert element.trap_reason == "placeholder"


# --- trap kind: trap_signature_email/_phone (staff signature block, docx) ---


def test_staff_signature_block_email_and_phone_are_flagged():
    text = (
        "1. Summary\nScheduled maintenance of the enrollment portal completed on "
        "2025-03-14. No member records were modified.\n\n"
        "2. Verification\nPost-maintenance checks passed; audit logging confirmed "
        "for all service accounts.\n\n"
        "Reported by: Dana Whitfield\nHR Benefits Coordinator, Meridian Benefits Group\n"
        "dana.whitfield@meridianbenefits.example\n(312) 555-0163"
    )
    elements = run_tier0(text)
    assert {e.element_type for e in elements} == {"email", "phone"}
    for element in elements:
        assert element.validation_status == "format_only"
        assert element.trap_reason == "signature_block"
        _assert_anchored(text, element)


def test_subject_contact_details_in_body_are_not_signature_flagged():
    # The same contact types OUTSIDE a signature block stay valid -- the
    # memo's From: header must not poison the first body sentence.
    text = (
        "To: Maria Alvarez\nFrom: Dana Whitfield, Payroll Specialist\n"
        "Date: 2025-03-14\nRe: Direct deposit verification\n\n"
        "The daytime contact number on file is (415) 555-0137.\n"
        "Confirmation was sent to maria.alvarez12@example.net."
    )
    elements = run_tier0(text)
    assert {e.element_type for e in elements} == {"phone", "email"}
    for element in elements:
        assert element.validation_status == "valid"
        assert element.trap_reason is None
