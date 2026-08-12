"""Shared fixture scenario for the B2 ER/exposure/review/export tests.

Builds one self-contained run in the real :5434 Postgres (UC2's DB-test
convention) exercising every path the B2 stage owns:

- exact-SSN auto-link (m1 "Robert Fournier" + m2 "Bob Fournier", same SSN)
- SharedName hard conflict (m3 "Robert Fournier", different SSN)
- gray-band nickname pair (m4 "Rob Fournier", same DOB, no identifier)
- transitive enemy blocking (m_e1/m_e2/m_e3 "Dana Ellison": e1-e2 share an
  email, e2-e3 share a phone, e1-e3 carry conflicting SSNs -- e1 and e3
  must never share a person)
- trap/invalid exclusion (format_only SSN + invalid_checksum card on m1)
- PartialIdentifiers: a mention-less cred-dump passage whose employee_id/
  SSN join keys attach its elements to Robert; plus an orphan phone that
  matches nobody and must park for review

Every document's bytes are a per-run uuid string, so sha256 values are
unique per scenario build; teardown deletes the run (documents/passages/
mentions/pii_elements/persons cascade) plus the JSONB-ref'd review_items /
er_decisions rows, which carry run_id in their refs precisely so cleanup
like this is one query.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    ErDecision,
    Mention,
    Passage,
    PiiElement,
    ProcessingRun,
    ReviewItem,
)
from app.services.er.normalize import normalize_value

ROBERT_SSN = "523-44-1234"
SHARED_NAME_SSN = "610-22-7777"
DANA_SSN_A = "447-12-3456"
DANA_SSN_B = "610-22-9911"
DANA_EMAIL = "dana.ellison@example.com"
DANA_PHONE = "(212) 555-0182"
EMPLOYEE_ID = "E12345"
CRED_PASSWORD = "hunter2"
ORPHAN_PHONE = "415-555-0000"
VALID_CARD = "4111 1111 1111 1111"
INVALID_CARD = "4111 1111 1111 1112"
TRAP_SSN = "531-24-8817"


@dataclass
class Scenario:
    run_id: uuid.UUID
    documents: dict[str, uuid.UUID] = field(default_factory=dict)
    passages: dict[str, uuid.UUID] = field(default_factory=dict)
    mentions: dict[str, uuid.UUID] = field(default_factory=dict)
    elements: dict[str, uuid.UUID] = field(default_factory=dict)


def _add_document(db: Session, run_id: uuid.UUID, filename: str) -> Document:
    content = f"{filename}:{uuid.uuid4().hex}".encode()
    import hashlib

    document = Document(
        run_id=run_id,
        sha256=hashlib.sha256(content).hexdigest(),
        original_filename=filename,
        rel_path=filename,
        byte_size=len(content),
        declared_mime="text/plain",
        sniffed_mime="text/plain",
        file_class="txt",
        source_kind="corpus",
        status="done",
    )
    db.add(document)
    db.flush()
    return document


def _add_passage(db: Session, document: Document, text: str) -> Passage:
    passage = Passage(
        document_id=document.id,
        seq=0,
        kind="text_block",
        locator={"block": 0},
        text=text,
    )
    db.add(passage)
    db.flush()
    return passage


def _add_mention(
    db: Session,
    document: Document,
    passage: Passage,
    name_raw: str,
    dob: date | None = None,
) -> Mention:
    from app.services.er.normalize import normalize_name

    mention = Mention(
        document_id=document.id,
        passage_id=passage.id,
        name_raw=name_raw,
        name_normalized=normalize_name(name_raw).full,
        dob=dob,
        features={},
        detector="llm_tier1",
        confidence=0.9,
    )
    db.add(mention)
    db.flush()
    return mention


def _add_element(
    db: Session,
    document: Document,
    passage: Passage,
    element_type: str,
    value_raw: str,
    *,
    mention: Mention | None = None,
    validation_status: str = "valid",
    detector: str = "llm_tier1",
    signals: dict | None = None,
) -> PiiElement:
    start = passage.text.find(value_raw)
    if start < 0:
        start = 0
    element = PiiElement(
        document_id=document.id,
        passage_id=passage.id,
        mention_id=mention.id if mention is not None else None,
        element_type=element_type,
        value_raw=value_raw,
        value_normalized=normalize_value(element_type, value_raw),
        char_start=start,
        char_end=start + len(value_raw),
        context_snippet=passage.text[max(0, start - 20) : start + len(value_raw) + 20],
        detector=detector,
        validation_status=validation_status,
        signals=signals,
    )
    db.add(element)
    db.flush()
    return element


def build_scenario(db: Session) -> Scenario:
    run = ProcessingRun(
        config_snapshot={"corpus_path": f"<er-scenario-{uuid.uuid4().hex[:8]}>"},
        status="finished",
    )
    db.add(run)
    db.flush()
    s = Scenario(run_id=run.id)

    # -- Robert Fournier: auto-link + traps + employee_id key ---------------
    d_hr = _add_document(db, run.id, "hr_record.txt")
    p_hr = _add_passage(
        db,
        d_hr,
        f"Robert Fournier, SSN {ROBERT_SSN}, born 1987-03-14. "
        f"Employee ID: {EMPLOYEE_ID}. Card on file {VALID_CARD} (backup {INVALID_CARD}).",
    )
    m1 = _add_mention(db, d_hr, p_hr, "Robert Fournier", dob=date(1987, 3, 14))
    s.elements["ssn_m1"] = _add_element(
        db, d_hr, p_hr, "ssn", ROBERT_SSN, mention=m1, detector="checksum"
    ).id
    s.elements["dob_m1"] = _add_element(db, d_hr, p_hr, "dob", "1987-03-14", mention=m1).id
    s.elements["card_valid_m1"] = _add_element(
        db, d_hr, p_hr, "credit_card", VALID_CARD, mention=m1, detector="checksum"
    ).id
    s.elements["card_invalid_m1"] = _add_element(
        db,
        d_hr,
        p_hr,
        "credit_card",
        INVALID_CARD,
        mention=m1,
        detector="checksum",
        validation_status="invalid_checksum",
    ).id
    s.elements["ssn_trap_m1"] = _add_element(
        db,
        d_hr,
        p_hr,
        "ssn",
        TRAP_SSN,
        mention=m1,
        detector="regex",
        validation_status="format_only",
        signals={"trap_reason": "order_number_context"},
    ).id

    d_memo = _add_document(db, run.id, "payroll_memo.txt")
    p_memo = _add_passage(db, d_memo, f"Payroll update for Bob Fournier (SSN {ROBERT_SSN}).")
    m2 = _add_mention(db, d_memo, p_memo, "Bob Fournier")
    s.elements["ssn_m2"] = _add_element(
        db, d_memo, p_memo, "ssn", ROBERT_SSN, mention=m2, detector="checksum"
    ).id

    # Second canonical printing so "Robert Fournier" is the MOST FREQUENT
    # form (2 vs 1) -- best_name selection must not depend on a tie-break.
    d_review = _add_document(db, run.id, "annual_review.txt")
    p_review = _add_passage(db, d_review, f"Annual review for Robert Fournier, SSN {ROBERT_SSN}.")
    m5 = _add_mention(db, d_review, p_review, "Robert Fournier")
    s.elements["ssn_m5"] = _add_element(
        db, d_review, p_review, "ssn", ROBERT_SSN, mention=m5, detector="checksum"
    ).id

    # -- SharedName collision: same printed name, different SSN -------------
    d_shared = _add_document(db, run.id, "other_robert.txt")
    p_shared = _add_passage(db, d_shared, f"Robert Fournier, SSN {SHARED_NAME_SSN}.")
    m3 = _add_mention(db, d_shared, p_shared, "Robert Fournier")
    s.elements["ssn_m3"] = _add_element(
        db, d_shared, p_shared, "ssn", SHARED_NAME_SSN, mention=m3, detector="checksum"
    ).id

    # -- gray-band nickname + dob pair ---------------------------------------
    d_gray = _add_document(db, run.id, "visitor_log.txt")
    p_gray = _add_passage(db, d_gray, "Rob Fournier (DOB 1987-03-14) attended the briefing.")
    m4 = _add_mention(db, d_gray, p_gray, "Rob Fournier", dob=date(1987, 3, 14))

    # -- Dana Ellison: transitive enemy chain --------------------------------
    d_e1 = _add_document(db, run.id, "dana_a.txt")
    p_e1 = _add_passage(db, d_e1, f"Dana Ellison, SSN {DANA_SSN_A}, {DANA_EMAIL}")
    m_e1 = _add_mention(db, d_e1, p_e1, "Dana Ellison")
    _add_element(db, d_e1, p_e1, "ssn", DANA_SSN_A, mention=m_e1, detector="checksum")
    _add_element(db, d_e1, p_e1, "email", DANA_EMAIL, mention=m_e1, detector="regex")

    d_e2 = _add_document(db, run.id, "dana_b.txt")
    p_e2 = _add_passage(db, d_e2, f"Dana Ellison <{DANA_EMAIL}> tel {DANA_PHONE}")
    m_e2 = _add_mention(db, d_e2, p_e2, "Dana Ellison")
    _add_element(db, d_e2, p_e2, "email", DANA_EMAIL, mention=m_e2, detector="regex")
    _add_element(db, d_e2, p_e2, "phone", DANA_PHONE, mention=m_e2, detector="checksum")

    d_e3 = _add_document(db, run.id, "dana_c.txt")
    p_e3 = _add_passage(db, d_e3, f"Dana Ellison tel {DANA_PHONE}, SSN {DANA_SSN_B}")
    m_e3 = _add_mention(db, d_e3, p_e3, "Dana Ellison")
    _add_element(db, d_e3, p_e3, "phone", DANA_PHONE, mention=m_e3, detector="checksum")
    _add_element(db, d_e3, p_e3, "ssn", DANA_SSN_B, mention=m_e3, detector="checksum")

    # -- PartialIdentifiers: cred dump with join keys, no mention ------------
    d_cred = _add_document(db, run.id, "cred_dump.txt")
    p_cred = _add_passage(
        db, d_cred, f"{EMPLOYEE_ID}:{CRED_PASSWORD}:{ROBERT_SSN} leaked in batch 7"
    )
    s.elements["cred_dump"] = _add_element(
        db, d_cred, p_cred, "credential", CRED_PASSWORD
    ).id
    s.elements["ssn_dump"] = _add_element(
        db, d_cred, p_cred, "ssn", ROBERT_SSN, detector="checksum"
    ).id

    # -- orphan element: attaches to nobody -> review parking ----------------
    d_orphan = _add_document(db, run.id, "orphan.txt")
    p_orphan = _add_passage(db, d_orphan, f"Callback requested at {ORPHAN_PHONE}.")
    s.elements["phone_orphan"] = _add_element(
        db, d_orphan, p_orphan, "phone", ORPHAN_PHONE, detector="checksum"
    ).id

    for key, value in {
        "hr": d_hr.id,
        "memo": d_memo.id,
        "shared": d_shared.id,
        "gray": d_gray.id,
        "cred": d_cred.id,
        "orphan": d_orphan.id,
    }.items():
        s.documents[key] = value
    for key, value in {
        "hr": p_hr.id,
        "memo": p_memo.id,
        "cred": p_cred.id,
        "orphan": p_orphan.id,
    }.items():
        s.passages[key] = value
    for key, value in {
        "m1": m1.id,
        "m2": m2.id,
        "m3": m3.id,
        "m4": m4.id,
        "m5": m5.id,
        "e1": m_e1.id,
        "e2": m_e2.id,
        "e3": m_e3.id,
    }.items():
        s.mentions[key] = value

    db.commit()
    return s


def teardown_scenario(db: Session, scenario: Scenario) -> None:
    run_id = str(scenario.run_id)
    db.execute(delete(ReviewItem).where(ReviewItem.ref["run_id"].astext == run_id))
    db.execute(delete(ErDecision).where(ErDecision.left_ref["run_id"].astext == run_id))
    db.execute(delete(ProcessingRun).where(ProcessingRun.id == scenario.run_id))
    db.commit()
