"""Conformance suite: every value format corpusgen generates has a tier-0
detector that finds it, byte-exact, in the sentence/table shapes
templates.py actually prints (docs/plan.md §15: "10 random manifest
plantings resolve to elements at correct offsets" -- this is the
generator-side half of that gate, run over hundreds of seeded values).

corpusgen lives at the repo root (it is a scored deliverable, not an app
module), so the repo root is added to sys.path here -- test-local, no
conftest side effects on other test files."""

import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from faker import Faker  # noqa: E402

from app.services.detectors import run_tier0, validators  # noqa: E402
from corpusgen import identities as ident  # noqa: E402
from corpusgen import templates  # noqa: E402
from corpusgen.config import MINI  # noqa: E402

SEED = 42  # the corpus seed (CLAUDE.md run command)


def _rng() -> random.Random:
    return random.Random(SEED)


def _detect_one(text: str, value: str, element_type: str) -> None:
    """Assert run_tier0 finds `value` in `text` as `element_type` with
    byte-exact offsets."""
    hits = [e for e in run_tier0(text) if e.element_type == element_type]
    assert len(hits) == 1, f"{element_type}: expected 1 hit in {text!r}, got {hits}"
    element = hits[0]
    assert element.value_raw == value
    assert text[element.char_start : element.char_end] == value
    assert element.trap_reason is None


# --- element makers: validators accept every generated value ----------------


def test_every_generated_ssn_is_structurally_valid_and_detected():
    rng, used = _rng(), set()
    for i in range(200):
        ssn = ident.make_ssn(rng, used)
        assert validators.ssn_is_structurally_valid(ssn.replace("-", ""))
        sentence = ident_sentence("ssn", ssn, i)
        _detect_one(sentence, ssn, "ssn")
        (element,) = [e for e in run_tier0(sentence) if e.element_type == "ssn"]
        assert element.validation_status == "valid"


def test_every_generated_card_is_luhn_valid_and_detected():
    rng, used = _rng(), set()
    for i in range(100):
        card = ident.make_credit_card(rng, used)
        assert validators.luhn_is_valid(card.replace(" ", ""))
        sentence = ident_sentence("credit_card", card, i)
        _detect_one(sentence, card, "credit_card")
        (element,) = [e for e in run_tier0(sentence) if e.element_type == "credit_card"]
        assert element.validation_status == "valid"


def test_every_generated_luhn_invalid_card_is_flagged():
    rng, used = _rng(), set()
    for _ in range(100):
        card = ident.make_luhn_invalid_card(rng, used)
        assert not validators.luhn_is_valid(card.replace(" ", ""))
        # The trap memo's exact sentence shape (templates.trap_card_memo).
        text = (
            f"The card number {card} submitted with the reimbursement request "
            "failed validation (invalid check digit) and was not charged."
        )
        hits = [e for e in run_tier0(text) if e.element_type == "credit_card"]
        assert len(hits) == 1
        assert hits[0].validation_status == "invalid_checksum"
        assert hits[0].trap_reason is None


def test_every_generated_phone_is_nanp_valid_and_detected():
    rng, used = _rng(), set()
    for i in range(100):
        phone = ident.make_phone(rng, used)
        digits = "".join(c for c in phone if c.isdigit())
        assert validators.phone_is_nanp_valid(digits)
        sentence = ident_sentence("phone", phone, i)
        _detect_one(sentence, phone, "phone")


def ident_sentence(element_type: str, value: str, i: int) -> str:
    """Cycle through the REAL template sentences for the type."""
    sentences = templates._SENTENCES[element_type]
    return sentences[i % len(sentences)].format(n="Maria Alvarez", v=value)


# --- full identity pool: every element family round-trips -------------------

# element key in Identity.elements -> tier-0 element_type (subset tier 0
# covers; dob/address/medical/username/password are tier-1 semantic types).
_TIER0_DETECTED = {
    "ssn": "ssn",
    "credit_card": "credit_card",
    "phone": "phone",
    "email": "email",
    "drivers_license": "drivers_license",
    "passport": "passport",
    "financial_account": "financial_account",
    "employee_id": "employee_id",
}


def _identity_pool():
    rng = _rng()
    Faker.seed(SEED)
    faker = Faker("en_US")
    return ident.generate_identities(rng, faker, MINI)


def test_every_identity_element_detected_in_its_template_sentence():
    pool = _identity_pool()
    rng = _rng()
    for person in pool.identities:
        for key, element_type in _TIER0_DETECTED.items():
            value = person.elements[key]
            sentences = templates._SENTENCES[key]
            text = rng.choice(sentences).format(n=person.canonical_name, v=value)
            _detect_one(text, value, element_type)


def test_every_identity_element_detected_in_claim_table_row():
    # medical_claim / support_ticket presentation: "Label | value".
    pool = _identity_pool()
    for person in pool.identities[:10]:
        for key, element_type in _TIER0_DETECTED.items():
            label = templates._CLAIM_LABELS[key]  # e.g. "Member SSN", "Member ID"
            value = person.elements[key]
            text = f"{label} | {value}"
            _detect_one(text, value, element_type)


def test_ssn_last4_from_generated_ssns():
    pool = _identity_pool()
    rng = _rng()
    for person in pool.identities:
        last4 = person.elements["ssn"][-4:]
        sentences = templates._SENTENCES["ssn_last4"]
        text = rng.choice(sentences).format(n=person.canonical_name, v=last4)
        hits = [e for e in run_tier0(text) if e.element_type == "ssn_last4"]
        assert len(hits) == 1, text
        assert hits[0].value_normalized == last4
        assert text[hits[0].char_start : hits[0].char_end] == last4


def test_tier1_only_element_families_produce_no_tier0_assertions():
    # dob / address / medical are tier-1 semantic territory: their
    # template sentences must not yield VALID tier-0 elements (a
    # format_only signal would be tolerable; a false 'valid' assertion
    # would not).
    pool = _identity_pool()
    rng = _rng()
    for person in pool.identities[:15]:
        for key in ("dob", "medical", "address"):
            value = person.dob if key == "dob" else person.elements[key]
            text = rng.choice(templates._SENTENCES[key]).format(
                n=person.canonical_name, v=value
            )
            for element in run_tier0(text):
                assert element.validation_status != "valid", (key, text, element)


def test_staff_signature_contacts_are_downgraded():
    # templates.make_staff generates the trap signature block's email and
    # phone; in the trap_signature_report layout both must come back
    # downgraded, never as clean subject PII.
    rng = _rng()
    Faker.seed(SEED)
    faker = Faker("en_US")
    for staff in templates.make_staff(rng, faker):
        text = (
            f"Reported by: {staff.name}\n{staff.title}, {templates.COMPANY}\n"
            f"{staff.email}\n{staff.phone}"
        )
        elements = run_tier0(text)
        assert {e.element_type for e in elements} == {"email", "phone"}, text
        for element in elements:
            assert element.validation_status == "format_only"
            assert element.trap_reason == "signature_block"
