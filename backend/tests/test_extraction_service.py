"""Extraction service tests against the real Postgres (UC2's convention
for DB-touching tests -- docker-compose :5434, migrations applied), with
FAKE adapters: every model interaction is scripted, ZERO live LLM calls.

Covers the §14b binding requirements (mentions.dob populated from dob
elements; employee_id lands in mention features, never pii_elements), D9
offset behavior (byte-located or review-flagged), the tier-0 agreement
boost, §9 escalation triggers (tier-0 disagreement -> tier-2 text; low OCR
confidence -> tier-2 vision; repair -> capped confidence -> degrade path),
the one-bounded-repair contract, per-call cost_events, and the §9
one-call-for-the-80-person-sheet rule with column projection."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import CostEvent, Mention, PiiElement
from app.repositories.documents import DocumentRepository
from app.repositories.passages import PassageRepository
from app.repositories.runs import ProcessingRunRepository
from app.services.extraction.base import ExtractionAdapter, ExtractionResult, ExtractionUsage
from app.services.extraction.service import run_extraction
from app.services.parsing.storage import store_page_image
from app.services.parsing.xlsx import build_header_map
from app.services.pipeline import _persist_tier0

# exp(-0.01) ~ 0.99: a confident fake response, well above theta.
_CONFIDENT_LOGPROBS = [{"logprob": -0.01}]


class FakeAdapter(ExtractionAdapter):
    """Scripted adapter: serves raw_json payloads in order and records every
    call -- the UC2 fake-adapter pattern; no transport, no keys, no network."""

    provider_id = "fake"
    supports_vision = True

    def __init__(self, payloads: list[dict], logprobs: list[dict] | None = _CONFIDENT_LOGPROBS):
        self._payloads = list(payloads)
        self._logprobs = logprobs
        self.calls: list[tuple[str, str]] = []

    def _next(self) -> ExtractionResult:
        assert self._payloads, "fake adapter exhausted -- unexpected extra call"
        return ExtractionResult(
            raw_json=self._payloads.pop(0),
            usage=ExtractionUsage(input_tokens=1000, output_tokens=100, cached_input_tokens=200),
            logprobs=self._logprobs,
        )

    async def extract_text(self, *, system_prompt, user_prompt, schema_json):
        self.calls.append(("text", user_prompt))
        return self._next()

    async def repair(self, *, system_prompt, user_prompt, schema_json):
        self.calls.append(("repair", user_prompt))
        return self._next()

    async def extract_image(self, *, system_prompt, user_prompt, image_png_b64, schema_json):
        self.calls.append(("image", user_prompt))
        return self._next()

    async def complete_json(self, *, system_prompt, user_prompt):
        self.calls.append(("json", user_prompt))
        return self._next()


@pytest.fixture()
def db():
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_document(db, file_class="txt"):
    run = ProcessingRunRepository(db).create(config_snapshot={"test": True})
    document = DocumentRepository(db).create(
        run_id=run.id,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_filename=f"test.{file_class}",
        rel_path=f"test.{file_class}",
        byte_size=1,
        file_class=file_class,
    )
    return run, document


def _add_passage(db, document, *, kind="text_block", locator=None, text="", seq=0, **kwargs):
    return PassageRepository(db).create(
        document_id=document.id,
        seq=seq,
        kind=kind,
        locator=locator or {"paragraph": seq + 1},
        text=text,
        ocr=kwargs.get("ocr", False),
        page_image_sha=kwargs.get("page_image_sha"),
    )


def _run(db, document, tier1, tier2=None):
    outcome = asyncio.run(
        run_extraction(db, document, tier1=tier1, tier2=tier2, settings=get_settings())
    )
    db.commit()
    return outcome


def _elements(db, document, detector=None):
    query = select(PiiElement).where(PiiElement.document_id == document.id)
    if detector:
        query = query.where(PiiElement.detector == detector)
    return list(db.scalars(query))


def _mentions(db, document):
    return list(db.scalars(select(Mention).where(Mention.document_id == document.id)))


def _cost_events(db, run):
    return list(db.scalars(select(CostEvent).where(CostEvent.run_id == run.id)))


# --- the happy path: dob, offsets, agreement, employee_id, cost events -------


TEXT = "Maria Alvarez (DOB: 03/14/1985), SSN 523-41-8722, badge E12345 on record."

HAPPY_PAYLOAD = {
    "mentions": [{"name_raw": "Maria Alvarez", "confidence": 1.0}],
    "elements": [
        {"element_type": "name", "value_raw": "Maria Alvarez", "mention_ref": 0, "confidence": 1.0},
        {"element_type": "dob", "value_raw": "03/14/1985", "mention_ref": 0, "confidence": 0.95},
        {"element_type": "ssn", "value_raw": "523-41-8722", "mention_ref": 0, "confidence": 0.98},
    ],
}


def test_happy_path_dob_offsets_agreement_features_costs(db):
    run, document = _make_document(db)
    passage = _add_passage(db, document, text=TEXT)
    _persist_tier0(db, document, [passage])  # tier-0 rows exist first (B1 contract)
    db.commit()

    tier1 = FakeAdapter([HAPPY_PAYLOAD])
    outcome = _run(db, document, tier1)

    # mentions.dob populated from the dob element via mention_ref (§14b BINDING)
    (mention,) = _mentions(db, document)
    assert mention.detector == "llm_tier1"
    assert mention.dob == date(1985, 3, 14)
    assert mention.name_normalized == "maria alvarez"

    # offsets located against the PASSAGE text (D9)
    llm_elements = _elements(db, document, detector="llm_tier1")
    assert len(llm_elements) == 3
    for element in llm_elements:
        assert passage.text[element.char_start : element.char_end] == element.value_raw
        assert element.passage_id == passage.id
        assert element.mention_id == mention.id
        assert not element.signals.get("offsets_missing")

    # tier-0 agreement boosts the ssn above type-mates with no tier-0 opinion
    by_type = {e.element_type: e for e in llm_elements}
    assert by_type["ssn"].signals["tier0_agreement"] is True
    assert by_type["dob"].signals["tier0_agreement"] is None
    assert float(by_type["ssn"].confidence) > float(by_type["dob"].confidence)

    # employee_id -> mention FEATURES, never pii_elements (§14b BINDING)
    assert mention.features["employee_ids"] == ["E12345"]
    assert not [e for e in _elements(db, document) if e.value_normalized == "E12345"]

    # every call -> one cost_events row, tier1 purpose, document-attributed
    events = _cost_events(db, run)
    assert len(events) == len(tier1.calls) == 1
    assert events[0].purpose == "tier1" and events[0].document_id == document.id
    assert float(events[0].cost_usd) > 0
    assert outcome.tier1_calls == 1 and outcome.tier2_calls == 0
    assert outcome.mentions == 1 and outcome.llm_elements == 3


# --- escalation: tier-0 valid hit the LLM missed -> tier-2 text --------------


def test_tier0_disagreement_escalates_to_tier2(db):
    run, document = _make_document(db)
    passage = _add_passage(db, document, text="Reference SSN 523-41-8722 in the claim file.")
    _persist_tier0(db, document, [passage])
    db.commit()

    tier1 = FakeAdapter([{"mentions": [], "elements": []}])  # misses the valid SSN
    tier2 = FakeAdapter(
        [
            {
                "mentions": [],
                "elements": [
                    {"element_type": "ssn", "value_raw": "523-41-8722", "mention_ref": None, "confidence": 0.9}
                ],
            }
        ],
        logprobs=None,  # the Anthropic tier exposes none -- composite renormalizes
    )
    outcome = _run(db, document, tier1, tier2)

    assert [c[0] for c in tier2.calls] == ["text"]
    (element,) = _elements(db, document, detector="llm_tier2")
    assert element.value_normalized == "523418722"
    assert element.signals["escalation_reason"] == "tier0_disagreement"
    assert passage.text[element.char_start : element.char_end] == element.value_raw

    purposes = sorted(e.purpose for e in _cost_events(db, run))
    assert purposes == ["tier1", "tier2_text"]
    assert outcome.escalations == 1
    assert any(step.get("reason") == "tier0_disagreement" for step in outcome.tier_path)


def test_escalation_without_tier2_degrades_to_review_flag(db):
    _run_repair_scenario(db, tier2=None)


# --- repair path (one bounded repair; repaired cap; degrade w/o tier-2) -----


def _run_repair_scenario(db, tier2):
    run, document = _make_document(db)
    passage = _add_passage(db, document, text="Note for Dana Brooks, member since 2019.")
    db.commit()

    invalid = {"mentions": [{"confidence": 1.0}], "elements": []}  # name_raw missing
    valid = {
        "mentions": [{"name_raw": "Dana Brooks", "confidence": 1.0}],
        "elements": [
            {"element_type": "name", "value_raw": "Dana Brooks", "mention_ref": 0, "confidence": 1.0}
        ],
    }
    tier1 = FakeAdapter([invalid, valid])
    outcome = _run(db, document, tier1, tier2)

    # exactly one bounded repair, both calls costed
    assert [c[0] for c in tier1.calls] == ["text", "repair"]
    tier1_events = [e for e in _cost_events(db, run) if e.purpose == "tier1"]
    assert len(tier1_events) == 2

    (element,) = _elements(db, document, detector="llm_tier1")
    assert element.signals["repaired"] is True
    # repaired cap (<= 0.75) drops the composite below theta (0.8) -> the
    # chunk becomes a low-confidence escalation candidate; with no tier-2
    # it degrades to a review flag instead of silence.
    assert float(element.confidence) <= 0.75
    assert element.signals["review"] is True
    assert element.signals["review_reason"] == "escalation_unavailable:low_confidence"
    assert outcome.escalations == 1 and outcome.review_flags >= 1
    assert passage.text[element.char_start : element.char_end] == "Dana Brooks"


# --- vision escalation (§14b R1: low-OCR image passages) ---------------------


def test_low_ocr_image_passage_routes_to_tier2_vision(db):
    run, document = _make_document(db, file_class="png")
    store_page_image(b"\x89PNG-fake-bytes", document.sha256, 1)
    _add_passage(
        db,
        document,
        kind="page",
        locator={"page": 1, "ocr_mean_conf": 52.0},
        text="j8#(f ksd8 3l1x garbled",
        ocr=True,
        page_image_sha=document.sha256,
    )
    db.commit()

    tier1 = FakeAdapter([])  # must never be called for this document
    tier2 = FakeAdapter(
        [
            {
                "mentions": [{"name_raw": "Priya Raman", "confidence": 0.9}],
                "elements": [
                    {"element_type": "name", "value_raw": "Priya Raman", "mention_ref": 0, "confidence": 0.9},
                    {"element_type": "ssn", "value_raw": "601-88-4415", "mention_ref": 0, "confidence": 0.85},
                ],
            }
        ],
        logprobs=None,
    )
    outcome = _run(db, document, tier1, tier2)

    assert tier1.calls == []
    assert [c[0] for c in tier2.calls] == ["image"]
    events = _cost_events(db, run)
    assert [e.purpose for e in events] == ["tier2_vision"]

    elements = _elements(db, document, detector="llm_tier2")
    assert len(elements) == 2
    for element in elements:
        # image-read values cannot anchor into garbage OCR text: degenerate
        # 0..0 span + review flag, never a fabricated offset (D9)
        assert element.signals["offsets_missing"] is True
        assert element.signals["review"] is True
        assert element.char_start == 0 and element.char_end == 0
        assert element.signals["vision"] is True
    (mention,) = _mentions(db, document)
    assert mention.detector == "llm_tier2"
    assert outcome.tier2_calls == 1 and outcome.tier1_calls == 0


def test_vision_without_tier2_flags_for_review_and_skips(db):
    run, document = _make_document(db, file_class="png")
    store_page_image(b"\x89PNG-fake-bytes", document.sha256, 1)
    _add_passage(
        db,
        document,
        kind="page",
        locator={"page": 1, "ocr_mean_conf": 51.0},
        text="garbage",
        ocr=True,
        page_image_sha=document.sha256,
    )
    db.commit()

    tier1 = FakeAdapter([])
    outcome = _run(db, document, tier1, tier2=None)
    assert tier1.calls == []
    assert outcome.review_flags == 1
    assert _cost_events(db, run) == []


# --- the 80-person sheet: one call + deterministic mentions + projection -----


def _sheet_document(db):
    run, document = _make_document(db, file_class="xlsx")
    headers = ["Name", "SSN", "DOB", "Email", "Notes"]
    header_map = build_header_map(headers)
    passages = []
    for block in range(2):  # rows 2-41 and 42-81, parsing/xlsx.py convention
        lines = [" | ".join(headers)]
        for i in range(block * 40, block * 40 + 40):
            lines.append(
                " | ".join(
                    [
                        f"Person{i} Smith",
                        f"523-41-{1000 + i:04d}",
                        "01/15/1980",
                        f"person{i}@example.com",
                        f"Diagnosed with condition {i}",
                    ]
                )
            )
        passages.append(
            _add_passage(
                db,
                document,
                kind="sheet_range",
                seq=block,
                locator={
                    "sheet": "Employees",
                    "row_start": block * 40 + 2,
                    "row_end": block * 40 + 41,
                    "header_map": header_map,
                },
                text="\n".join(lines),
            )
        )
    _persist_tier0(db, document, passages)
    db.commit()
    return run, document, passages


def test_sheet_costs_one_call_with_deterministic_mentions_and_projection(db):
    run, document, passages = _sheet_document(db)

    sample_payload = {
        "mentions": [],
        "elements": [
            {
                "element_type": "medical",
                "value_raw": f"Diagnosed with condition {i}",
                "mention_ref": None,
                "confidence": 0.9,
            }
            for i in range(5)  # the sampled rows (2-6)
        ],
    }
    tier1 = FakeAdapter([sample_payload])
    outcome = _run(db, document, tier1)

    # THE §9 rule: the whole 80-person sheet costs exactly one tier-1 call,
    # and the model saw only the unmapped column.
    assert len(tier1.calls) == 1
    assert outcome.tier1_calls == 1
    user_prompt = tier1.calls[0][1]
    assert "Notes" in user_prompt and "Diagnosed with condition 0" in user_prompt
    assert "523-41-1000" not in user_prompt and "person0@example.com" not in user_prompt
    assert len(_cost_events(db, run)) == 1

    # one deterministic mention per data row, dob populated from the mapped column
    mentions = _mentions(db, document)
    assert len(mentions) == 80
    assert all(m.detector == "deterministic_sheet" for m in mentions)
    assert all(m.dob == date(1980, 1, 15) for m in mentions)

    # tier-0 rows (ssn/email) got attached to their row's mention
    tier0_rows = [e for e in _elements(db, document) if e.detector in ("regex", "checksum")]
    ssn_rows = [e for e in tier0_rows if e.element_type == "ssn"]
    assert len(ssn_rows) == 80
    assert all(e.mention_id is not None for e in ssn_rows)

    # the sampled column's semantic got PROJECTED onto all 80 rows,
    # each grounded in its own cell and attached to its row mention
    medical = [e for e in _elements(db, document, detector="llm_tier1") if e.element_type == "medical"]
    assert len(medical) == 80
    passage_by_id = {p.id: p for p in passages}
    for element in medical:
        assert element.signals["projected_from_sample"] is True
        assert element.mention_id is not None
        text = passage_by_id[element.passage_id].text
        assert text[element.char_start : element.char_end] == element.value_raw

    # mapped non-tier-0 columns became deterministic elements (dob per row)
    dob_rows = [e for e in tier0_rows if e.element_type == "dob"]
    assert len(dob_rows) == 80
    assert all(e.signals["source"] == "sheet_header_map" for e in dob_rows)
