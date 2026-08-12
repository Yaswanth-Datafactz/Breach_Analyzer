"""Shared object-graph builder for the agent-layer tests.

Builds one processing run with everything the four agents touch: mention
pairs for every adjudication case (corroborated merge, hard-conflict,
name-only, bulk cluster), an exposure flag with passage-anchored evidence
for the auditor, and an open wrong-extension quarantine (real xlsx bytes in
the content-addressed store) for the investigator.

Convention notes:
- runs against the real docker-compose Postgres on :5434 (UC2/B1 test
  convention -- see tests/test_models_smoke.py).
- every stored file embeds a uuid so sha256 values never collide across
  test sessions (documents.sha256 is globally UNIQUE -- plan §14b R2).
- teardown deletes everything the tests created: agent runs registered on
  env.agent_run_ids (cost_events first, approvals/steps/tool_calls cascade),
  er_decisions/review_items by reference, then the processing run (documents,
  passages, elements, mentions, persons, links, flags, evidence cascade).
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field

import pymupdf
from openpyxl import Workbook
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    CostEvent,
    Document,
    ErDecision,
    ExposureFlag,
    FlagEvidence,
    IdentityLink,
    Mention,
    Passage,
    Person,
    PiiElement,
    ProcessingRun,
    Quarantine,
    ReviewItem,
)
from app.services.parsing import storage
from app.services.parsing.storage import sha256_of


@dataclass
class AgentEnv:
    run: ProcessingRun
    doc_a: Document
    doc_b: Document
    doc_pdf: Document
    doc_q: Document
    quarantine: Quarantine
    passage_a: Passage
    passage_flag: Passage
    m_bob: Mention
    m_robert: Mention
    m_liz1: Mention
    m_liz2: Mention
    m_name1: Mention
    m_name2: Mention
    m_bulk_anchor: Mention
    m_bulk_new: Mention
    person_bulk: Person
    person_flag: Person
    flag: ExposureFlag
    flag_element: PiiElement
    mention_ids: list[str]
    agent_run_ids: list[uuid.UUID] = field(default_factory=list)


def _xlsx_bytes(marker: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Employees"
    sheet.append(["Name", "Note"])
    sheet.append(["Casey Vance", marker])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_bytes(marker: str) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), f"OCR TARGET {marker}", fontsize=14)
    return pdf.tobytes()


def _document(
    db: Session,
    run: ProcessingRun,
    *,
    filename: str,
    content: bytes,
    file_class: str,
    status: str = "done",
) -> Document:
    sha = sha256_of(content)
    from pathlib import Path

    storage.store_original(content, sha, Path(filename).suffix)
    doc = Document(
        run_id=run.id,
        sha256=sha,
        original_filename=filename,
        rel_path=f"agents-tests/{filename}",
        byte_size=len(content),
        file_class=file_class,
        status=status,
    )
    db.add(doc)
    db.flush()
    return doc


def _passage(db: Session, doc: Document, seq: int, text: str) -> Passage:
    passage = Passage(
        document_id=doc.id, seq=seq, kind="text_block", locator={"block": seq}, text=text
    )
    db.add(passage)
    db.flush()
    return passage


def _mention(
    db: Session, doc: Document, passage: Passage, name: str, elements: list[tuple[str, str, str]]
) -> Mention:
    mention = Mention(
        document_id=doc.id,
        passage_id=passage.id,
        name_raw=name,
        name_normalized=name.lower(),
        detector="llm_tier1",
        confidence=0.9,
    )
    db.add(mention)
    db.flush()
    for element_type, value_raw, value_normalized in elements:
        db.add(
            PiiElement(
                document_id=doc.id,
                passage_id=passage.id,
                mention_id=mention.id,
                element_type=element_type,
                value_raw=value_raw,
                value_normalized=value_normalized,
                char_start=0,
                char_end=len(value_raw),
                detector="llm_tier1",
                validation_status="valid",
            )
        )
    db.flush()
    return mention


def build_agent_env(db: Session) -> AgentEnv:
    uid = uuid.uuid4().hex[:12]
    run = ProcessingRun(config_snapshot={"agents_test": uid}, status="finished")
    db.add(run)
    db.flush()

    doc_a = _document(
        db, run, filename=f"memo_{uid}.txt", content=f"memo {uid}".encode(), file_class="txt"
    )
    doc_b = _document(
        db, run, filename=f"letter_{uid}.txt", content=f"letter {uid}".encode(), file_class="txt"
    )
    doc_pdf = _document(
        db,
        run,
        filename=f"scan_{uid}.pdf",
        content=_pdf_bytes(uid),
        file_class="pdf_digital",
    )
    # The wrong-extension trap: real xlsx bytes stored under a .pdf name.
    doc_q = _document(
        db,
        run,
        filename=f"report_{uid}.pdf",
        content=_xlsx_bytes(uid),
        file_class="pdf_digital",
        status="quarantined",
    )
    quarantine = Quarantine(
        document_id=doc_q.id,
        stage="parse",
        reason_code="parser_error",
        detail="pymupdf.FileDataError: cannot open broken document",
    )
    db.add(quarantine)
    db.flush()

    passage_a = _passage(db, doc_a, 0, f"Employee memo {uid}: Robert Smith, SSN 523-11-9876.")
    passage_b = _passage(db, doc_b, 0, f"Letter {uid}: Bob Smith called about SSN 523-11-9876.")

    # Corroborated pair (shared SSN) -- a legal merge.
    m_bob = _mention(db, doc_b, passage_b, "Bob Smith", [("ssn", "523-11-9876", "523119876")])
    m_robert = _mention(
        db, doc_a, passage_a, "Robert Smith", [("ssn", "523-11-9876", "523119876")]
    )
    # Hard-conflict pair (same name, DIFFERENT SSNs) -- must never merge.
    m_liz1 = _mention(
        db, doc_a, passage_a, "Elizabeth Tran", [("ssn", "412-55-0001", "412550001")]
    )
    m_liz2 = _mention(
        db, doc_b, passage_b, "Elizabeth Tran", [("ssn", "498-77-0002", "498770002")]
    )
    # Name-only pair (zero corroboration) -- must never merge.
    m_name1 = _mention(db, doc_a, passage_a, "Jordan Casey", [])
    m_name2 = _mention(db, doc_b, passage_b, "Jordan Casey", [])

    # Bulk cluster: one person with 11 active links; merging one more trips
    # the >10 bulk_merge gate. Anchor and newcomer share an email so the
    # merge is corroborated (the gate, not the constraints, must fire).
    person_bulk = Person(run_id=run.id, best_name="Bulk Sheet Person", review_status="auto")
    db.add(person_bulk)
    db.flush()
    m_bulk_anchor = _mention(
        db, doc_a, passage_a, "Bulk Sheet Person",
        [("email", f"bulk_{uid}@example.com", f"bulk_{uid}@example.com")],
    )
    bulk_mentions = [m_bulk_anchor]
    for i in range(10):
        bulk_mentions.append(_mention(db, doc_a, passage_a, f"Bulk Sheet Person {i}", []))
    for m in bulk_mentions:
        db.add(
            IdentityLink(
                person_id=person_bulk.id, mention_id=m.id, score=0.95,
                method="rule", rule_id="exact_email", active=True,
            )
        )
    m_bulk_new = _mention(
        db, doc_b, passage_b, "B. S. Person",
        [("email", f"bulk_{uid}@example.com", f"bulk_{uid}@example.com")],
    )
    person_bulk.mention_count = 11
    db.flush()

    # Auditor graph: a flag whose evidence anchors to a real passage.
    person_flag = Person(run_id=run.id, best_name="Dana Whitfield", review_status="auto")
    db.add(person_flag)
    db.flush()
    passage_flag = _passage(
        db, doc_a, 1, f"Benefits record {uid}. Employee SSN: 523-88-1234 on file."
    )
    flag_element = PiiElement(
        document_id=doc_a.id,
        passage_id=passage_flag.id,
        element_type="ssn",
        value_raw="523-88-1234",
        value_normalized="523881234",
        char_start=passage_flag.text.index("523-88-1234"),
        char_end=passage_flag.text.index("523-88-1234") + len("523-88-1234"),
        detector="regex",
        validation_status="valid",
    )
    db.add(flag_element)
    db.flush()
    flag = ExposureFlag(person_id=person_flag.id, category="ssn", exposed=True, confidence=0.85)
    db.add(flag)
    db.flush()
    db.add(
        FlagEvidence(
            exposure_flag_id=flag.id,
            pii_element_id=flag_element.id,
            document_id=doc_a.id,
            passage_id=passage_flag.id,
            snippet="Employee SSN: 523-88-1234 on file.",
        )
    )
    db.commit()

    return AgentEnv(
        run=run,
        doc_a=doc_a,
        doc_b=doc_b,
        doc_pdf=doc_pdf,
        doc_q=doc_q,
        quarantine=quarantine,
        passage_a=passage_a,
        passage_flag=passage_flag,
        m_bob=m_bob,
        m_robert=m_robert,
        m_liz1=m_liz1,
        m_liz2=m_liz2,
        m_name1=m_name1,
        m_name2=m_name2,
        m_bulk_anchor=m_bulk_anchor,
        m_bulk_new=m_bulk_new,
        person_bulk=person_bulk,
        person_flag=person_flag,
        flag=flag,
        flag_element=flag_element,
        mention_ids=[
            str(m.id)
            for m in (m_bob, m_robert, m_liz1, m_liz2, m_name1, m_name2, m_bulk_anchor, m_bulk_new)
        ]
        + [str(m.id) for m in bulk_mentions],
    )


def teardown_agent_env(db: Session, env: AgentEnv) -> None:
    db.rollback()
    if env.agent_run_ids:
        db.execute(delete(CostEvent).where(CostEvent.agent_run_id.in_(env.agent_run_ids)))
        db.execute(delete(AgentRun).where(AgentRun.id.in_(env.agent_run_ids)))
    db.execute(
        delete(ErDecision).where(
            ErDecision.left_ref["mention_id"].astext.in_(env.mention_ids)
        )
    )
    db.execute(
        delete(ReviewItem).where(
            ReviewItem.ref["exposure_flag_id"].astext == str(env.flag.id)
        )
    )
    db.execute(
        delete(ReviewItem).where(ReviewItem.ref["left_mention_id"].astext.in_(env.mention_ids))
    )
    db.execute(delete(ProcessingRun).where(ProcessingRun.id == env.run.id))
    db.commit()
