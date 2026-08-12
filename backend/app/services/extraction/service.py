"""Tier-1/tier-2 extraction over one document (docs/plan.md §3 pipeline
stage, §9 routing rules, §14b B2 binding requirements).

`run_extraction(db, document, ...)` assumes B1 already ran: passages are
persisted and tier-0 detector rows exist in pii_elements. It then:

1. Runs the DETERMINISTIC SHEET PASS over sheet_range passages whose
   locator carries the header_map (parsing/xlsx.py's hook): one mention
   per data row from the name column (detector='deterministic_sheet',
   §4), mentions.dob from the mapped dob column, deterministic elements
   for mapped columns tier-0's regexes cannot type (dob/address/medical/
   credential -- detector='regex' with signals.source=sheet_header_map:
   the CHECK constraint's closed detector set has no deterministic-sheet
   member for elements, and a header-lexicon lookup is the same
   no-model-involved family as a regex), and attaches the row's existing
   tier-0 elements to the row mention. This is what makes "the 80-person
   sheet costs ~ one tier-1 call" true: the call only ever covers
   unmapped columns (chunker.py).
2. Chunks everything else (chunker.py) and calls tier 1 per chunk with
   prompts v2, Pydantic-validates against PassageExtraction with ONE
   bounded repair (§3), computes offsets by string-match against the
   passage text (D9, extraction/offsets.py via chunker.locate_in_chunk),
   and scores every element with the composite (services/confidence.py).
3. Reconciles tier 0 vs tier 1: an agreeing valid tier-0 hit boosts the
   composite; a valid tier-0 hit the LLM missed makes the chunk a
   DISAGREEMENT escalation candidate (§9).
4. Escalates per §9: invalid-after-repair / min composite < theta /
   disagreement -> tier-2 TEXT re-extraction of the chunk (persisted as
   detector='llm_tier2', replacing the tier-1 read for that chunk);
   image passages with low ocr_mean_conf were already routed by the
   chunker to tier-2 VISION (§14b R1 -- the BulkSpreadsheet PNG path).
   With no tier-2 adapter configured, escalation degrades to persisting
   the tier-1 read with a review flag in signals -- degraded, never
   silent.
5. Populates mentions.dob from dob elements carrying a mention_ref
   (§14b BINDING: ER blocking/features consume it), and re-runs the pure
   tier-0 detectors to collect employee_id hits into mention FEATURES
   JSONB -- §14b BINDING: employee_id is the PartialIdentifiers ER join
   key and is NOT a §4 element category, so it never lands in
   pii_elements.
6. Writes one cost_events row per LLM call through the shared recorder
   (extraction/costing.py) -- purposes tier1 / tier2_text / tier2_vision
   per §4.

Elements whose value_raw cannot be byte-located get char offsets 0..0
(the columns are NOT NULL by §4 design -- the degenerate empty span is
the explicit "no anchor" encoding) plus signals.offsets_missing=true and
signals.review=true, which is the review-queue flag (§4 review_items
kind=extraction consumes these in B3).
"""

from __future__ import annotations

import asyncio
import base64
import math
import uuid
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Document, Passage, PiiElement
from app.repositories.mentions import MentionRepository
from app.repositories.passages import PassageRepository
from app.repositories.pii_elements import PiiElementRepository
from app.services.confidence import (
    ElementSignals,
    composite_confidence,
    logprob_signal,
    rollup,
)
from app.services.detectors import run_tier0
from app.services.detectors import validators as detector_validators
from app.services.extraction import chunker
from app.services.extraction.base import ExtractionAdapter, ExtractionResult
from app.services.extraction.chunker import (
    Chunk,
    ChunkLocatedElement,
    cell_offsets,
    sheet_separator,
    split_sheet_rows,
)
from app.services.extraction.costing import record_cost_event
from app.services.extraction.prompts import build_repair_prompt, build_tier1_prompt
from app.services.extraction.schemas import ElementType, PassageExtraction
from app.services.parsing import storage
from app.services.er.normalize import normalize_dob, normalize_value

logger = get_logger("extraction.service")

# Element types tier 0 has a detector for -- the set over which the
# tier0/tier1 agreement signal is defined (anything else is None ->
# renormalized away, services/confidence.py).
_TIER0_DETECTABLE = frozenset(
    {"ssn", "ssn_last4", "credit_card", "phone", "email", "passport", "financial_account", "drivers_license"}
)

# Types whose validation_status is a real computed verdict (checksum /
# structural / syntax -- detectors/validators.py); only these feed the
# composite's validator signal, everything else scores format_only in the
# COLUMN but None in the composite.
_VALIDATED_TYPES = frozenset({"ssn", "credit_card", "phone", "email"})

_CONTEXT_SNIPPET_CHARS = 40

_SCHEMA_JSON = PassageExtraction.model_json_schema()


@dataclass
class ExtractionOutcome:
    """What the pipeline rolls into extraction_jobs + run counters."""

    mentions: int = 0
    llm_elements: int = 0
    tier1_calls: int = 0
    tier2_calls: int = 0
    escalations: int = 0
    review_flags: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    tier_path: list = field(default_factory=list)


@dataclass
class _SheetRow:
    passage: Passage
    sheet: str
    row_number: int
    line_offset: int
    cells: list[str]
    # The separator passage.text actually uses (chunker.sheet_separator):
    # " | " for xlsx sheet chunks, "," for csv raw lines -- cell offset
    # arithmetic is only correct with the real one.
    separator: str
    mention_id: uuid.UUID | None = None
    name_offset: int = 0


@dataclass
class _EvaluatedElement:
    located: ChunkLocatedElement
    composite: float
    signals: dict


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_extraction(
    db: Session,
    document: Document,
    *,
    tier1: ExtractionAdapter,
    tier2: ExtractionAdapter | None,
    settings: Settings,
) -> ExtractionOutcome:
    outcome = ExtractionOutcome()
    passages, _total = PassageRepository(db).list_for_document(document.id, limit=10_000)
    if not passages:
        return outcome

    tier0_by_passage = _load_tier0_elements(db, document.id)
    mention_repo = MentionRepository(db)
    element_repo = PiiElementRepository(db)
    # (passage_id, char anchor, mention_id) for every mention created in
    # this run -- the employee_id feature pass attaches by proximity.
    mention_anchors: list[tuple[uuid.UUID, int, uuid.UUID]] = []

    sheet_rows = _deterministic_sheet_pass(
        db, document, passages, tier0_by_passage, mention_repo, element_repo, outcome, mention_anchors
    )

    chunks = chunker.build_chunks(
        passages,
        max_tokens=settings.extraction_chunk_max_tokens,
        sheet_sample_rows=settings.sheet_sample_rows,
        vision_conf_threshold=settings.vision_ocr_conf_threshold,
    )
    text_chunks = sum(1 for c in chunks if c.kind != chunker.VISION)
    if text_chunks:
        outcome.tier_path.append(
            {"tier": 1, "model": settings.deepseek_model, "chunks": text_chunks}
        )

    for chunk in chunks:
        try:
            if chunk.kind == chunker.VISION:
                await _process_vision_chunk(
                    db, document, chunk, tier2, settings, outcome, mention_repo, element_repo,
                    tier0_by_passage, mention_anchors,
                )
            elif chunk.kind == chunker.SHEET_SAMPLE:
                await _process_sheet_chunk(
                    db, document, chunk, tier1, settings, outcome, mention_repo, element_repo,
                    tier0_by_passage, mention_anchors, sheet_rows,
                )
            else:
                await _process_text_chunk(
                    db, document, chunk, tier1, tier2, settings, outcome, mention_repo,
                    element_repo, tier0_by_passage, mention_anchors,
                )
        except Exception as exc:  # one chunk must never sink the document
            logger.exception(
                "chunk_extraction_failed", document_id=str(document.id), chunk_kind=chunk.kind
            )
            outcome.review_flags += 1
            outcome.tier_path.append(
                {"tier": 1, "chunk_kind": chunk.kind, "error": f"{type(exc).__name__}: {exc}"}
            )

    _attach_employee_id_features(db, passages, mention_repo, mention_anchors)
    return outcome


# ---------------------------------------------------------------------------
# Deterministic sheet pass (§9 deterministic-first; §4 detector
# 'deterministic_sheet' for mentions)
# ---------------------------------------------------------------------------


def _deterministic_sheet_pass(
    db: Session,
    document: Document,
    passages: list[Passage],
    tier0_by_passage: dict,
    mention_repo: MentionRepository,
    element_repo: PiiElementRepository,
    outcome: ExtractionOutcome,
    mention_anchors: list,
) -> dict[str, list[_SheetRow]]:
    rows_by_sheet: dict[str, list[_SheetRow]] = {}
    for passage in passages:
        locator = passage.locator or {}
        header_map = locator.get("header_map")
        if passage.kind != "sheet_range" or not header_map:
            continue
        sheet = locator.get("sheet", "")
        separator = sheet_separator(passage)
        name_col = next((e["col"] for e in header_map if e.get("element_type") == "name"), None)
        dob_col = next((e["col"] for e in header_map if e.get("element_type") == "dob"), None)

        for row_number, line_offset, cells in split_sheet_rows(passage):
            row = _SheetRow(
                passage=passage, sheet=sheet, row_number=row_number,
                line_offset=line_offset, cells=cells, separator=separator,
            )
            rows_by_sheet.setdefault(sheet, []).append(row)
            if not any(cell.strip() for cell in cells):
                continue

            name_cell = cells[name_col - 1].strip() if name_col and name_col <= len(cells) else ""
            if name_cell:
                dob_value = None
                if dob_col and dob_col <= len(cells) and cells[dob_col - 1].strip():
                    dob_value = _parse_dob(cells[dob_col - 1].strip())
                mention = mention_repo.create(
                    document_id=document.id,
                    passage_id=passage.id,
                    name_raw=name_cell,
                    detector="deterministic_sheet",
                    dob=dob_value,
                    confidence=1.0,
                    features={"sheet": sheet, "row": row_number},
                )
                outcome.mentions += 1
                row.mention_id = mention.id
                name_start, _ = cell_offsets(line_offset, cells, name_col, separator=separator)
                row.name_offset = name_start
                mention_anchors.append((passage.id, name_start, mention.id))

            line_end = line_offset + len(separator.join(cells))

            # Attach the row's tier-0 hits (ssn/email/... already persisted
            # by B1) to the row's mention -- evidence stays on the original
            # tier-0 row, ER gets the person link.
            if row.mention_id is not None:
                for t0 in tier0_by_passage.get(passage.id, []):
                    if t0.mention_id is None and line_offset <= t0.char_start < line_end:
                        t0.mention_id = row.mention_id

            # Mapped columns tier 0 cannot type (plus any mapped tier-0 type
            # its regexes missed) become deterministic element rows.
            for entry in header_map:
                element_type = entry.get("element_type")
                col = entry["col"]
                if element_type is None or col > len(cells):
                    continue
                cell = cells[col - 1].strip()
                if not cell:
                    continue
                start, end = cell_offsets(line_offset, cells, col, separator=separator)
                if passage.text[start:end].strip() != cell:
                    # D9 discipline: a wrong anchor is worse than a missing
                    # one. Offset arithmetic can only miss when a source
                    # cell embeds the separator (e.g. a quoted csv cell) --
                    # skip it and let review/tier-1 carry that cell.
                    logger.warning(
                        "sheet_cell_offset_mismatch",
                        document_id=str(document.id),
                        sheet=sheet,
                        row=row_number,
                        column=col,
                    )
                    continue
                if element_type in _TIER0_DETECTABLE and any(
                    t0.char_start >= start and t0.char_end <= end
                    for t0 in tier0_by_passage.get(passage.id, [])
                ):
                    continue  # tier 0 already owns this cell's evidence
                normalized = normalize_value(element_type, cell)
                element_repo.create(
                    document_id=document.id,
                    passage_id=passage.id,
                    element_type=element_type,
                    value_raw=cell,
                    value_normalized=normalized[:500],
                    char_start=start,
                    char_end=end,
                    detector="regex",
                    validation_status=detector_validators.status_for(element_type, normalized),
                    context_snippet=_snippet(passage.text, start, end),
                    mention_id=row.mention_id,
                    confidence=1.0,
                    signals={"source": "sheet_header_map", "column": entry.get("header")},
                )
        db.flush()
    return rows_by_sheet


def _parse_dob(raw: str) -> date | None:
    iso = normalize_dob(raw)
    if iso is None:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:  # pragma: no cover - normalize_dob emits ISO
        return None


# ---------------------------------------------------------------------------
# LLM call plumbing (validate + one bounded repair, every call costed)
# ---------------------------------------------------------------------------


async def _call_validated(
    db: Session,
    adapter: ExtractionAdapter,
    *,
    chunk_text: str,
    purpose: str,
    model: str,
    document: Document,
    outcome: ExtractionOutcome,
    image_png_b64: str | None = None,
) -> tuple[PassageExtraction | None, ExtractionResult, bool]:
    """(parsed | None, last raw result, went_through_repair). Exactly one
    repair attempt ever happens (§3); both calls write cost_events."""
    system_prompt, user_prompt = build_tier1_prompt(chunk_text)
    if image_png_b64 is not None:
        result = await adapter.extract_image(
            system_prompt=system_prompt, user_prompt=user_prompt,
            image_png_b64=image_png_b64, schema_json=_SCHEMA_JSON,
        )
    else:
        result = await adapter.extract_text(
            system_prompt=system_prompt, user_prompt=user_prompt, schema_json=_SCHEMA_JSON
        )
    _record(db, document, purpose, model, result, outcome)
    try:
        return PassageExtraction.model_validate(result.raw_json), result, False
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(part) for part in e["loc"]), "msg": e["msg"]}
            for e in exc.errors()
        ]
        repair_system, repair_user = build_repair_prompt(result.raw_json, errors, chunk_text)
        repair_result = await adapter.repair(
            system_prompt=repair_system, user_prompt=repair_user, schema_json=_SCHEMA_JSON
        )
        _record(db, document, purpose, model, repair_result, outcome)
        try:
            return PassageExtraction.model_validate(repair_result.raw_json), repair_result, True
        except ValidationError:
            logger.warning(
                "invalid_after_repair", document_id=str(document.id), purpose=purpose
            )
            return None, repair_result, True


def _record(
    db: Session,
    document: Document,
    purpose: str,
    model: str,
    result: ExtractionResult,
    outcome: ExtractionOutcome,
) -> None:
    event = record_cost_event(
        db,
        run_id=document.run_id,
        purpose=purpose,
        model=model,
        usage=result.usage,
        document_id=document.id,
    )
    if purpose == "tier1":
        outcome.tier1_calls += 1
    else:
        outcome.tier2_calls += 1
    outcome.input_tokens += result.usage.input_tokens
    outcome.output_tokens += result.usage.output_tokens
    outcome.cached_input_tokens += result.usage.cached_input_tokens
    outcome.cost_usd = round(outcome.cost_usd + float(event.cost_usd), 6)


# ---------------------------------------------------------------------------
# Evaluation (offsets + composite) and persistence
# ---------------------------------------------------------------------------


def _evaluate_elements(
    chunk: Chunk,
    parsed: PassageExtraction,
    logprobs: list[dict] | None,
    repaired: bool,
    tier0_by_passage: dict,
) -> list[_EvaluatedElement]:
    located = chunker.locate_in_chunk(chunk, parsed.elements)
    call_logprob = logprob_signal(logprobs)
    evaluated: list[_EvaluatedElement] = []
    for item in located:
        element_type = item.element.element_type.value
        normalized = normalize_value(element_type, item.element.value_raw)
        agreement = (
            _tier0_agreement(item.passage, element_type, normalized, tier0_by_passage)
            if item.passage is not None
            else None
        )
        signals = ElementSignals(
            grounded=not item.offsets_missing,
            logprob=call_logprob,
            tier0_agreement=agreement,
            validator_status=(
                detector_validators.status_for(element_type, normalized)
                if element_type in _VALIDATED_TYPES
                else None
            ),
            repaired=repaired,
        )
        composite = composite_confidence(signals)
        evaluated.append(
            _EvaluatedElement(
                located=item,
                composite=composite,
                signals={
                    "composite": composite,
                    "grounded": signals.grounded,
                    "logprob": signals.logprob,
                    "tier0_agreement": signals.tier0_agreement,
                    "validator": signals.validator_status,
                    "repaired": repaired,
                    "model_confidence": float(item.element.confidence),
                },
            )
        )
    return evaluated


def _tier0_agreement(
    passage: Passage, element_type: str, normalized: str, tier0_by_passage: dict
) -> bool | None:
    """True: tier 0 independently found the same value, untainted. False:
    tier 0 saw the value but flagged it as a trap -- the LLM extracting it
    anyway is a disagreement that LOWERS confidence. None: tier 0 has no
    detector for the type, or simply no matching hit (no opinion)."""
    if element_type not in _TIER0_DETECTABLE:
        return None
    matches = [
        t0
        for t0 in tier0_by_passage.get(passage.id, [])
        if t0.element_type == element_type and t0.value_normalized == normalized
    ]
    if not matches:
        return None
    if any((t0.signals or {}).get("trap_reason") for t0 in matches):
        return False
    return True


def _persist_extraction(
    db: Session,
    document: Document,
    chunk: Chunk,
    parsed: PassageExtraction,
    evaluated: list[_EvaluatedElement],
    *,
    detector: str,
    mention_repo: MentionRepository,
    element_repo: PiiElementRepository,
    outcome: ExtractionOutcome,
    mention_anchors: list,
    review_reason: str | None = None,
    extra_signals: dict | None = None,
) -> None:
    """Write mentions + elements for one validated chunk extraction.
    Mention passage attribution follows the located NAME element for that
    mention (falling back to any located element, then the chunk's first
    span); mentions.dob comes from dob elements with mention_ref (§14b
    binding)."""
    fallback_passage = chunk.spans[0].passage if chunk.spans else None
    by_ref: dict[int, list[_EvaluatedElement]] = {}
    for item in evaluated:
        ref = item.located.element.mention_ref
        if ref is not None:
            by_ref.setdefault(ref, []).append(item)

    mention_ids: list[uuid.UUID | None] = []
    for index, mention_out in enumerate(parsed.mentions):
        candidates = by_ref.get(index, [])
        anchor = next(
            (
                c.located
                for c in candidates
                if c.located.element.element_type == ElementType.NAME and not c.located.offsets_missing
            ),
            None,
        ) or next((c.located for c in candidates if not c.located.offsets_missing), None)
        passage = anchor.passage if anchor is not None else fallback_passage
        if passage is None:  # pragma: no cover - chunks always carry spans
            mention_ids.append(None)
            continue
        mention = mention_repo.create(
            document_id=document.id,
            passage_id=passage.id,
            name_raw=mention_out.name_raw,
            detector=detector,
            confidence=float(mention_out.confidence),
        )
        outcome.mentions += 1
        mention_ids.append(mention.id)
        mention_anchors.append(
            (passage.id, anchor.char_start if anchor is not None else 0, mention.id)
        )

    # mentions.dob from dob elements with mention_ref (§14b BINDING).
    for item in evaluated:
        element = item.located.element
        if element.element_type is not ElementType.DOB or element.mention_ref is None:
            continue
        mention_id = mention_ids[element.mention_ref]
        if mention_id is None:
            continue
        dob_value = _parse_dob(element.value_raw)
        if dob_value is None:
            continue
        mention = mention_repo.get(mention_id)
        if mention is not None and mention.dob is None:
            mention_repo.set_dob(mention, dob_value)

    for item in evaluated:
        element = item.located.element
        element_type = element.element_type.value
        passage = item.located.passage or fallback_passage
        if passage is None:  # pragma: no cover - chunks always carry spans
            continue
        normalized = normalize_value(element_type, element.value_raw)
        signals = dict(item.signals)
        if extra_signals:
            signals.update(extra_signals)
        if item.located.offsets_missing:
            # D9: never guess an anchor -- flag for review instead. The NOT
            # NULL offset columns get the explicit degenerate 0..0 span.
            signals["offsets_missing"] = True
            signals["review"] = True
            outcome.review_flags += 1
        if review_reason:
            signals["review"] = True
            signals["review_reason"] = review_reason
            outcome.review_flags += 1
        char_start = item.located.char_start if not item.located.offsets_missing else 0
        char_end = item.located.char_end if not item.located.offsets_missing else 0
        mention_id = (
            mention_ids[element.mention_ref]
            if element.mention_ref is not None and element.mention_ref < len(mention_ids)
            else None
        )
        element_repo.create(
            document_id=document.id,
            passage_id=passage.id,
            element_type=element_type,
            value_raw=element.value_raw,
            value_normalized=normalized[:500],
            char_start=char_start,
            char_end=char_end,
            detector=detector,
            validation_status=detector_validators.status_for(element_type, normalized),
            context_snippet=(
                _snippet(passage.text, char_start, char_end)
                if not item.located.offsets_missing
                else None
            ),
            mention_id=mention_id,
            confidence=item.composite,
            signals=signals,
        )
        outcome.llm_elements += 1
    db.flush()


def _snippet(text: str, start: int, end: int) -> str:
    return text[max(0, start - _CONTEXT_SNIPPET_CHARS) : end + _CONTEXT_SNIPPET_CHARS]


# ---------------------------------------------------------------------------
# Chunk processors
# ---------------------------------------------------------------------------


async def _process_text_chunk(
    db, document, chunk, tier1, tier2, settings, outcome, mention_repo, element_repo,
    tier0_by_passage, mention_anchors,
) -> None:
    parsed, result, repaired = await _call_validated(
        db, tier1, chunk_text=chunk.text, purpose="tier1",
        model=settings.deepseek_model, document=document, outcome=outcome,
    )

    evaluated: list[_EvaluatedElement] = []
    escalation_reason: str | None = None
    if parsed is None:
        escalation_reason = "invalid_after_repair"
    else:
        evaluated = _evaluate_elements(chunk, parsed, result.logprobs, repaired, tier0_by_passage)
        if evaluated and rollup(e.composite for e in evaluated) < settings.tier2_escalation_threshold:
            escalation_reason = "low_confidence"
        elif _tier0_valid_missed(chunk, parsed, tier0_by_passage):
            escalation_reason = "tier0_disagreement"

    if escalation_reason is None:
        _persist_extraction(
            db, document, chunk, parsed, evaluated, detector="llm_tier1",
            mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
            mention_anchors=mention_anchors,
        )
        return

    outcome.escalations += 1
    outcome.tier_path.append(
        {"tier": 2, "model": settings.tier2_model, "reason": escalation_reason}
    )
    if tier2 is None:
        # Degrade gracefully (never silently): keep the tier-1 read, flagged.
        logger.info(
            "tier2_unavailable", document_id=str(document.id), reason=escalation_reason
        )
        if parsed is not None:
            _persist_extraction(
                db, document, chunk, parsed, evaluated, detector="llm_tier1",
                mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
                mention_anchors=mention_anchors,
                review_reason=f"escalation_unavailable:{escalation_reason}",
            )
        else:
            outcome.review_flags += 1
        return

    parsed2, result2, repaired2 = await _call_validated(
        db, tier2, chunk_text=chunk.text, purpose="tier2_text",
        model=settings.tier2_model, document=document, outcome=outcome,
    )
    if parsed2 is not None:
        evaluated2 = _evaluate_elements(chunk, parsed2, result2.logprobs, repaired2, tier0_by_passage)
        _persist_extraction(
            db, document, chunk, parsed2, evaluated2, detector="llm_tier2",
            mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
            mention_anchors=mention_anchors,
            extra_signals={"escalation_reason": escalation_reason},
        )
    elif parsed is not None:
        _persist_extraction(
            db, document, chunk, parsed, evaluated, detector="llm_tier1",
            mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
            mention_anchors=mention_anchors, review_reason="tier2_invalid_after_repair",
        )
    else:
        logger.warning("chunk_unextractable", document_id=str(document.id))
        outcome.review_flags += 1
        outcome.tier_path.append({"tier": 2, "error": "invalid_after_repair_both_tiers"})


def _tier0_valid_missed(chunk: Chunk, parsed: PassageExtraction, tier0_by_passage: dict) -> bool:
    """§9 disagreement trigger: a clean, checksum-VALID tier-0 hit inside
    this chunk's passages that tier 1 did not report."""
    llm_values = {
        (e.element_type.value, normalize_value(e.element_type.value, e.value_raw))
        for e in parsed.elements
    }
    for span in chunk.spans:
        for t0 in tier0_by_passage.get(span.passage.id, []):
            if t0.validation_status != "valid" or (t0.signals or {}).get("trap_reason"):
                continue
            if (t0.element_type, t0.value_normalized) not in llm_values:
                return True
    return False


async def _process_sheet_chunk(
    db, document, chunk, tier1, settings, outcome, mention_repo, element_repo,
    tier0_by_passage, mention_anchors, sheet_rows,
) -> None:
    """ONE tier-1 call for the whole sheet (§9): the sample decides what the
    unmapped columns mean; the decided semantic is then PROJECTED
    deterministically onto every row, each projected element grounded in
    its own cell by exact offsets. Sheet chunks never escalate -- the
    deterministic extraction stands regardless, and a weak column vote
    simply doesn't project (rows keep no speculative elements)."""
    parsed, result, repaired = await _call_validated(
        db, tier1, chunk_text=chunk.text, purpose="tier1",
        model=settings.deepseek_model, document=document, outcome=outcome,
    )
    if parsed is None:
        outcome.review_flags += 1
        outcome.tier_path.append(
            {"tier": 1, "chunk_kind": chunker.SHEET_SAMPLE, "error": "invalid_after_repair"}
        )
        return

    rows = sheet_rows.get(chunk.sheet or "", [])
    sampled = [row for row in rows if row.row_number in set(chunk.sampled_row_numbers)]

    # Column vote: which element_type did the model assign to each unmapped
    # column's sampled cells?
    votes: dict[int, dict[str, int]] = {}
    sampled_cells: dict[int, int] = {}
    projected_value_pool: set[str] = set()
    for row in sampled:
        for col in chunk.unmapped_columns:
            if col > len(row.cells) or not row.cells[col - 1].strip():
                continue
            cell = row.cells[col - 1]
            sampled_cells[col] = sampled_cells.get(col, 0) + 1
            for element in parsed.elements:
                if element.value_raw == cell:
                    votes.setdefault(col, {})[element.element_type.value] = (
                        votes.setdefault(col, {}).get(element.element_type.value, 0) + 1
                    )
                    break

    projected_columns: dict[int, str] = {}
    for col, col_votes in votes.items():
        element_type, count = max(col_votes.items(), key=lambda kv: kv[1])
        if count >= max(1, math.ceil(sampled_cells.get(col, 0) / 2)):
            projected_columns[col] = element_type
            projected_value_pool.update(
                row.cells[col - 1]
                for row in sampled
                if col <= len(row.cells) and row.cells[col - 1].strip()
            )

    call_logprob = logprob_signal(result.logprobs)
    for col, element_type in projected_columns.items():
        for row in rows:
            if col > len(row.cells) or not row.cells[col - 1].strip():
                continue
            cell = row.cells[col - 1]
            start, end = cell_offsets(row.line_offset, row.cells, col, separator=row.separator)
            if row.passage.text[start:end] != cell:  # pragma: no cover - arithmetic guard
                continue
            normalized = normalize_value(element_type, cell)
            signals = ElementSignals(
                grounded=True,
                logprob=call_logprob,
                tier0_agreement=None,
                validator_status=(
                    detector_validators.status_for(element_type, normalized)
                    if element_type in _VALIDATED_TYPES
                    else None
                ),
                repaired=repaired,
            )
            composite = composite_confidence(signals)
            element_repo.create(
                document_id=document.id,
                passage_id=row.passage.id,
                element_type=element_type,
                value_raw=cell,
                value_normalized=normalized[:500],
                char_start=start,
                char_end=end,
                detector="llm_tier1",
                validation_status=detector_validators.status_for(element_type, normalized),
                context_snippet=_snippet(row.passage.text, start, end),
                mention_id=row.mention_id,
                confidence=composite,
                signals={
                    "composite": composite,
                    "projected_from_sample": True,
                    "grounded": True,
                    "logprob": call_logprob,
                    "repaired": repaired,
                },
            )
            outcome.llm_elements += 1
        logger.info(
            "sheet_column_projected",
            document_id=str(document.id),
            sheet=chunk.sheet,
            column=col,
            element_type=element_type,
            rows=len(rows),
        )
    db.flush()

    # Whatever the sample yielded beyond the projected columns (an unusual
    # value, a mention) persists through the normal path -- minus elements
    # already covered by a projection, which would otherwise double-write.
    residual = PassageExtraction(
        mentions=parsed.mentions,
        elements=[e for e in parsed.elements if e.value_raw not in projected_value_pool],
    )
    if residual.mentions or residual.elements:
        evaluated = _evaluate_elements(chunk, residual, result.logprobs, repaired, tier0_by_passage)
        _persist_extraction(
            db, document, chunk, residual, evaluated, detector="llm_tier1",
            mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
            mention_anchors=mention_anchors,
        )


async def _process_vision_chunk(
    db, document, chunk, tier2, settings, outcome, mention_repo, element_repo,
    tier0_by_passage, mention_anchors,
) -> None:
    """§9: image page + low OCR confidence -> tier-2 VISION (§14b R1: the
    BulkSpreadsheet PNGs are invisible to tier 0/1 -- this path carries
    them). Elements extracted from the image usually cannot byte-anchor
    into the garbage OCR text; those persist with the 0..0 span +
    offsets_missing/review signals rather than a fabricated anchor."""
    passage = chunk.spans[0].passage
    if tier2 is None or not tier2.supports_vision:
        logger.info(
            "vision_escalation_unavailable",
            document_id=str(document.id),
            passage_id=str(passage.id),
        )
        outcome.review_flags += 1
        outcome.tier_path.append(
            {"tier": 2, "vision": True, "error": "no_vision_adapter"}
        )
        return

    page_no = (passage.locator or {}).get("page", 1)
    image_path = storage.page_image_path(passage.page_image_sha, page_no)
    if not image_path.exists():
        logger.warning(
            "page_image_missing", document_id=str(document.id), passage_id=str(passage.id)
        )
        outcome.review_flags += 1
        return
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")

    outcome.escalations += 1
    outcome.tier_path.append(
        {"tier": 2, "vision": True, "model": settings.tier2_model, "reason": "low_ocr_confidence"}
    )
    parsed, result, repaired = await _call_validated(
        db, tier2, chunk_text=chunk.text, purpose="tier2_vision",
        model=settings.tier2_model, document=document, outcome=outcome,
        image_png_b64=image_b64,
    )
    if parsed is None:
        outcome.review_flags += 1
        return
    evaluated = _evaluate_elements(chunk, parsed, result.logprobs, repaired, tier0_by_passage)
    _persist_extraction(
        db, document, chunk, parsed, evaluated, detector="llm_tier2",
        mention_repo=mention_repo, element_repo=element_repo, outcome=outcome,
        mention_anchors=mention_anchors, extra_signals={"vision": True},
    )


# ---------------------------------------------------------------------------
# employee_id -> mention features (§14b BINDING: never a pii_elements row)
# ---------------------------------------------------------------------------


def _attach_employee_id_features(
    db: Session,
    passages: list[Passage],
    mention_repo: MentionRepository,
    mention_anchors: list[tuple[uuid.UUID, int, uuid.UUID]],
) -> None:
    """Re-run the pure tier-0 detectors for employee_id hits (the
    PartialIdentifiers ER join key) and fold them into the NEAREST mention's
    features JSONB in the same passage. run_tier0 is free and pure, so a
    re-run here is cheaper than threading detector state through B1."""
    anchors_by_passage: dict[uuid.UUID, list[tuple[int, uuid.UUID]]] = {}
    for passage_id, offset, mention_id in mention_anchors:
        anchors_by_passage.setdefault(passage_id, []).append((offset, mention_id))

    collected: dict[uuid.UUID, set[str]] = {}
    for passage in passages:
        hits = [h for h in run_tier0(passage.text) if h.element_type == "employee_id"]
        if not hits:
            continue
        anchors = anchors_by_passage.get(passage.id)
        if not anchors:
            logger.info(
                "employee_id_unattached",
                passage_id=str(passage.id),
                hits=len(hits),
            )
            continue
        for hit in hits:
            _offset, mention_id = min(anchors, key=lambda a: abs(a[0] - hit.char_start))
            collected.setdefault(mention_id, set()).add(hit.value_normalized)

    for mention_id, values in collected.items():
        mention = mention_repo.get(mention_id)
        if mention is None:  # pragma: no cover - ids came from this session
            continue
        existing = set((mention.features or {}).get("employee_ids", []))
        mention_repo.merge_features(mention, {"employee_ids": sorted(existing | values)})


# ---------------------------------------------------------------------------
# Tier-0 element loading
# ---------------------------------------------------------------------------


def _load_tier0_elements(db: Session, document_id: uuid.UUID) -> dict[uuid.UUID, list[PiiElement]]:
    """Read the document's tier-0 rows grouped by passage. A direct select
    rather than a repository method: PiiElementRepository is owned by the
    B1 track and this phase's path boundary keeps its file untouched -- the
    write side still goes through the repository."""
    rows = db.scalars(
        select(PiiElement).where(
            PiiElement.document_id == document_id,
            PiiElement.detector.in_(("regex", "checksum")),
        )
    )
    grouped: dict[uuid.UUID, list[PiiElement]] = {}
    for row in rows:
        grouped.setdefault(row.passage_id, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# Sync wrapper for the pipeline's worker threads
# ---------------------------------------------------------------------------


def run_extraction_sync(
    db: Session,
    document: Document,
    *,
    tier1: ExtractionAdapter,
    tier2: ExtractionAdapter | None,
    settings: Settings,
) -> ExtractionOutcome:
    """The pipeline's parse workers are sync functions in threads
    (services/pipeline.py concurrency note); each owns its own event loop
    for the async adapter calls."""
    return asyncio.run(
        run_extraction(db, document, tier1=tier1, tier2=tier2, settings=settings)
    )
