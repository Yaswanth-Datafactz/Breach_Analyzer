"""Bulk pipeline orchestration (docs/plan.md D2: UC2's proven async
job-runner pattern -- status rows + startup stale-job reaper; Celery/arq
named as the explicit 100x-load upgrade path in §12, not built now).

Stages: INGEST (inventory -> sha256 dedup -> classify -> route or
quarantine, originals stored content-addressed) -> PARSE (per-type parser;
email attachments recursively re-ingested as child documents) -> passages
persisted -> tier-0 detectors IF the parallel detectors track has landed
(try-import; skipped with a log line otherwise) -> B2's tier-1/tier-2
EXTRACTION stage (services/extraction/service.py) under the SAME
degrade-gracefully contract as the tier-0 try-import: with no
DEEPSEEK_API_KEY configured the stage is skipped -- logged once per run --
and documents finish `done` at tier-0 depth, so frontend/dev data keeps
working and the live keyed run later is config-only. Each extracted
document gets an extraction_jobs row (status/tier_path/token+cost rollups;
the one-live-job partial unique index guards double-dispatch).

Failure semantics ("nothing silently dropped", CLAUDE.md):
- No exception ever escapes a single document. Known file-is-broken
  exception types map to a `corrupt` quarantine, anything else to
  `parser_error` with the exception detail -- either way the run continues
  and the reconciliation invariant (every document `done` or quarantined)
  holds.
- Run-level counters on processing_runs are updated + committed after every
  document, so GET /runs/{id} is live while the run executes.

Concurrency: the parse stage is CPU-bound at the OCR extreme, so each
document is processed in a worker thread (asyncio.to_thread) behind a small
semaphore (settings.extraction_max_concurrency -- the same 3-5 knob plan
§14's full-corpus run uses). Every worker owns its own Session; the
coordinator serializes counter updates in the event loop, so no two writers
ever touch the run row concurrently.

Dedup (plan §9 rule 2): documents.sha256 is UNIQUE per run (migration 002,
docs/plan.md §14b R2), so identical content WITHIN a run -- the same
attachment on five emails, a duplicated corpus file -- is stored and parsed
exactly once; later sightings are counted in the run's `deduped` counter
and logged with both paths. Across runs, rows are independent (re-processing
a corpus just works); byte storage stays globally content-addressed.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pikepdf
import pymupdf
from PIL import UnidentifiedImageError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Document, ExtractionJob
from app.db.session import SessionLocal
from app.repositories.documents import DocumentRepository
from app.repositories.passages import PassageRepository
from app.repositories.pii_elements import PiiElementRepository
from app.repositories.processing import ProcessingRepository
from app.repositories.quarantines import QuarantineRepository
from app.services.extraction import get_tier1_adapter, get_tier2_adapter
from app.services.extraction.service import ExtractionOutcome, run_extraction_sync
from app.services.ingestion import classify
from app.services.ingestion.inventory import (
    declared_mime_for,
    iter_corpus_files,
    sha256_of,
    sniff_mime,
)
from app.services.parsing import storage
from app.services.parsing.csv_ import parse_csv
from app.services.parsing.docx import parse_docx
from app.services.parsing.eml import parse_eml
from app.services.parsing.html_txt import parse_html, parse_txt
from app.services.parsing.image_ocr import parse_png
from app.services.parsing.pdf import garbage_ratio, parse_pdf
from app.services.parsing.types import ParseResult
from app.services.parsing.xlsx import parse_xlsx

logger = get_logger("pipeline")

# The tier-0 detectors are a parallel track (docs/plan.md §14) -- integrated
# behind a try-import so B1 keeps landing independently even if the module
# is temporarily absent. `run_tier0(passage_text) -> list[DetectedElement]`
# is pure (its module docstring: "the pipeline owns that persistence"), so
# _persist_tier0 below maps hits onto pii_elements rows.
try:
    from app.services.detectors import context as detector_context
    from app.services.detectors import run_tier0
    from app.services.detectors import validators as detector_validators
except ImportError:  # pragma: no cover - only while the detectors track is absent
    run_tier0 = None
    detector_context = None  # type: ignore[assignment]
    detector_validators = None  # type: ignore[assignment]

# Element types whose validation_status comes from a computable checksum/
# structural rule (Luhn, SSN area-group rules, NANP) -- their decided hits
# are attributed detector='checksum'; everything else (shape-only matches,
# syntax-only email validation) stays detector='regex'.
_CHECKSUM_VALIDATED_TYPES = frozenset({"ssn", "credit_card", "phone"})

_CONTEXT_SNIPPET_CHARS = 40

# Exception types that mean "the file itself is broken" -- mapped to the
# `corrupt` quarantine reason; every other parser exception is
# `parser_error` (an us-problem until proven otherwise, and the exception
# detail is preserved for the investigator agent either way).
_CORRUPT_EXCEPTIONS: tuple[type[Exception], ...] = (
    zipfile.BadZipFile,
    pikepdf.PdfError,
    pymupdf.FileDataError,
    UnidentifiedImageError,
)

_COUNTER_KEYS = (
    "files_seen",
    "ingested",
    "deduped",
    "attachments",
    "parsed",
    "done",
    "quarantined",
    "failed",
    "passages",
    "tier0_elements",
    "ocr_documents",
    # B2 extraction stage (0 whenever the stage is skipped -- no key)
    "extracted",
    "mentions",
    "llm_elements",
    "tier1_calls",
    "tier2_calls",
)


def build_config_snapshot(*, corpus_path: str) -> dict:
    """The replayability anchor (plan §4): models, thresholds, git_sha,
    corpus path -- everything needed to attribute this run's outputs to the
    exact code + config that produced them."""
    settings = get_settings()
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        git_sha = None
    return {
        "corpus_path": corpus_path,
        "git_sha": git_sha,
        "models": {
            "tier1": settings.deepseek_model,
            "tier2": settings.tier2_model,
            "agent": settings.agent_model,
        },
        "thresholds": {
            "tier2_escalation": settings.tier2_escalation_threshold,
            "er_auto_link": settings.er_auto_link_threshold,
            "er_distinct": settings.er_distinct_threshold,
            "ocr_min_chars_per_page": settings.ocr_min_chars_per_page,
            "ocr_max_garbage_ratio": settings.ocr_max_garbage_ratio,
            "extraction_chunk_max_tokens": settings.extraction_chunk_max_tokens,
            "sheet_sample_rows": settings.sheet_sample_rows,
            "vision_ocr_conf_threshold": settings.vision_ocr_conf_threshold,
        },
        "concurrency": settings.extraction_max_concurrency,
    }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@dataclass
class _IngestResult:
    queued_ids: list[uuid.UUID] = field(default_factory=list)
    files_seen: int = 0
    ingested: int = 0
    deduped: int = 0
    quarantined: int = 0


def _ingest_one(
    db: Session,
    *,
    run_id: uuid.UUID,
    rel_path: str,
    filename: str,
    content: bytes,
    sha256: str,
    declared_mime: str | None,
    sniffed_mime: str,
    source_kind: str = "corpus",
    parent_document_id: uuid.UUID | None = None,
) -> tuple[Document | None, bool]:
    """Ingest one file (corpus or attachment): dedup check, content-
    addressed store, classify, documents row (+ quarantine row when the
    route says so). Returns (document|None, was_duplicate) -- None document
    means the content was already known and no new row was created."""
    documents = DocumentRepository(db)
    existing = documents.get_by_sha256(sha256, run_id=run_id)
    if existing is not None:
        logger.info(
            "ingest_deduped",
            rel_path=rel_path,
            existing_document_id=str(existing.id),
            existing_rel_path=existing.rel_path,
            sha256=sha256,
        )
        return None, True

    storage.store_original(content, sha256, Path(filename).suffix)

    decision = classify.route(
        filename=filename,
        byte_size=len(content),
        declared_mime=declared_mime,
        sniffed_mime=sniffed_mime,
        content=content,
    )

    stored_sniffed_mime = decision.sniffed_mime_note or sniffed_mime
    document = documents.create(
        run_id=run_id,
        sha256=sha256,
        original_filename=filename,
        rel_path=rel_path,
        byte_size=len(content),
        declared_mime=declared_mime,
        sniffed_mime=stored_sniffed_mime,
        file_class=decision.file_class,
        source_kind=source_kind,
        parent_document_id=parent_document_id,
    )

    if decision.quarantine_reason is not None:
        document.status = "quarantined"
        QuarantineRepository(db).create(
            document_id=document.id,
            stage="ingest",
            reason_code=decision.quarantine_reason,
            detail=decision.detail,
        )
        logger.info(
            "ingest_quarantined",
            document_id=str(document.id),
            rel_path=rel_path,
            reason_code=decision.quarantine_reason,
        )
    elif decision.reclassified:
        logger.info(
            "ingest_reclassified",
            document_id=str(document.id),
            rel_path=rel_path,
            file_class=decision.file_class,
            sniffed_mime=stored_sniffed_mime,
        )

    return document, False


def _ingest_corpus(run_id: uuid.UUID, corpus_dir: Path) -> _IngestResult:
    """Sync ingest pass over the corpus dir (runs in a worker thread).
    Commits after every file so GET /runs/{id} counters are live during
    ingest, not only during parse."""
    result = _IngestResult()
    db = SessionLocal()
    try:
        processing = ProcessingRepository(db)
        run = processing.get_run(run_id)
        assert run is not None  # caller verified existence before dispatch
        for item, content in iter_corpus_files(corpus_dir):
            result.files_seen += 1
            document, was_duplicate = _ingest_one(
                db,
                run_id=run_id,
                rel_path=item.rel_path,
                filename=item.filename,
                content=content,
                sha256=item.sha256,
                declared_mime=item.declared_mime,
                sniffed_mime=item.sniffed_mime,
            )
            if was_duplicate:
                result.deduped += 1
            elif document is not None:
                result.ingested += 1
                if document.status == "quarantined":
                    result.quarantined += 1
                else:
                    result.queued_ids.append(document.id)
            counters = dict(run.counters or {})
            counters.update(
                {
                    "files_seen": result.files_seen,
                    "ingested": result.ingested,
                    "deduped": result.deduped,
                    "quarantined": result.quarantined,
                }
            )
            processing.set_counters(run, counters)
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


# ---------------------------------------------------------------------------
# Parse (per document, worker thread)
# ---------------------------------------------------------------------------


@dataclass
class _DocumentOutcome:
    document_id: uuid.UUID
    status: str  # 'done' | 'quarantined' | 'failed'
    passages: int = 0
    tier0_elements: int = 0
    ocr: bool = False
    child_queued_ids: list[uuid.UUID] = field(default_factory=list)
    child_ingested: int = 0
    child_deduped: int = 0
    child_quarantined: int = 0
    # B2 extraction stage rollups (all zero when the stage is skipped)
    extracted: bool = False
    mentions: int = 0
    llm_elements: int = 0
    tier1_calls: int = 0
    tier2_calls: int = 0


def _cross_passage_trap_reason(persisted: list, index: int, hit) -> str | None:
    """Trap context the pure per-passage detectors cannot see (measured on
    the full-corpus B1 run -- both cases leaked staff contacts as
    validation_status='valid' without this):

    - eml headers: email/phone hits in the {'part': 'headers'} passage are
      the document HANDLER's From/To routing addresses (the manifest plants
      only trap_staff_email there, never subject PII). run_tier0 sees bare
      text; the locator is the structural evidence, so the downgrade
      belongs here.
    - docx paragraph split: parse_docx emits one passage per paragraph
      (corpusgen's {paragraph: n} location vocabulary), so a signature
      marker ('Reported by:', 'Thanks,') lands 1-3 passages BEFORE the
      staff email/phone it must downgrade. Stitch the preceding paragraphs
      back together and re-evaluate the same context heuristics at the
      hit's stitched offset -- passage granularity must never change trap
      semantics.
    """
    passage = persisted[index]
    if (
        passage.kind == "email_part"
        and (passage.locator or {}).get("part") == "headers"
        and hit.element_type in ("email", "phone")
    ):
        return detector_context.TRAP_STAFF_HEADER
    if passage.kind == "text_block" and index > 0:
        prefix = "\n".join(p.text for p in persisted[:index])
        return detector_context.trap_reason_for(
            f"{prefix}\n{passage.text}",
            len(prefix) + 1 + hit.char_start,
            hit.element_type,
        )
    return None


def _persist_tier0(db: Session, document: Document, persisted: list) -> int:
    """Run the free tier-0 detectors over every persisted passage and write
    the hits as pii_elements rows (detector attribution per
    _CHECKSUM_VALIDATED_TYPES; trap_reason lands in `signals` for the
    tier-1 disagreement check and the trap scorecard). employee_id hits are
    skipped here: not a §4 element category -- they are the
    PartialIdentifiers ER join key, consumed by B2's mention building from
    a re-run of the (pure, free) detectors."""
    if run_tier0 is None:
        logger.info(
            "tier0_skipped", document_id=str(document.id), reason="detectors_not_landed"
        )
        return 0

    elements = PiiElementRepository(db)
    count = 0
    for index, passage in enumerate(persisted):
        for hit in run_tier0(passage.text):
            if hit.element_type == "employee_id":
                continue
            validation_status = hit.validation_status
            trap_reason = hit.trap_reason
            if trap_reason is None:
                cross_reason = _cross_passage_trap_reason(persisted, index, hit)
                if cross_reason is not None:
                    trap_reason = cross_reason
                    validation_status = detector_validators.FORMAT_ONLY
            decided = validation_status in ("valid", "invalid_checksum")
            detector = (
                "checksum"
                if decided and hit.element_type in _CHECKSUM_VALIDATED_TYPES
                else "regex"
            )
            snippet_start = max(0, hit.char_start - _CONTEXT_SNIPPET_CHARS)
            elements.create(
                document_id=document.id,
                passage_id=passage.id,
                element_type=hit.element_type,
                value_raw=hit.value_raw,
                value_normalized=hit.value_normalized,
                char_start=hit.char_start,
                char_end=hit.char_end,
                detector=detector,
                validation_status=validation_status,
                context_snippet=passage.text[
                    snippet_start : hit.char_end + _CONTEXT_SNIPPET_CHARS
                ],
                signals={"trap_reason": trap_reason} if trap_reason else None,
            )
            count += 1
    return count


def _run_parser(parser_id: str, content: bytes, sha256: str) -> ParseResult:
    if parser_id == "pdf":
        return parse_pdf(content, sha256=sha256)
    if parser_id == "png":
        return parse_png(content, sha256=sha256)
    dispatch = {
        "docx": parse_docx,
        "xlsx": parse_xlsx,
        "csv": parse_csv,
        "eml": parse_eml,
        "html": parse_html,
        "txt": parse_txt,
    }
    return dispatch[parser_id](content)


def _ingest_attachments(
    db: Session, document: Document, result: ParseResult, outcome: _DocumentOutcome
) -> None:
    """Recursively re-ingest email attachments as child documents (plan §3):
    same classify/quarantine/dedup path as corpus files, with
    parent_document_id + source_kind='attachment'. Newly-queued children are
    fed back to the coordinator, which schedules them like any other
    document -- an attached .eml's own attachments recurse naturally."""
    for attachment in result.attachments:
        sha256 = sha256_of(attachment.content)
        child, was_duplicate = _ingest_one(
            db,
            run_id=document.run_id,
            rel_path=f"{document.rel_path}!{attachment.filename}",
            filename=attachment.filename,
            content=attachment.content,
            sha256=sha256,
            declared_mime=declared_mime_for(attachment.filename),
            sniffed_mime=sniff_mime(attachment.content),
            source_kind="attachment",
            parent_document_id=document.id,
        )
        if was_duplicate:
            outcome.child_deduped += 1
        elif child is not None:
            outcome.child_ingested += 1
            if child.status == "quarantined":
                outcome.child_quarantined += 1
            else:
                outcome.child_queued_ids.append(child.id)


def _quarantine_document(
    db: Session, document: Document, *, reason_code: str, detail: str
) -> None:
    ProcessingRepository(db).set_document_status(document, "quarantined")
    QuarantineRepository(db).create(
        document_id=document.id, stage="parse", reason_code=reason_code, detail=detail
    )
    logger.info(
        "parse_quarantined",
        document_id=str(document.id),
        rel_path=document.rel_path,
        reason_code=reason_code,
    )


def _ocr_garbage_detail(result: ParseResult) -> str | None:
    """Post-OCR quality gate: an image-based document whose OCR output is
    mostly garbage is evidence-useless -- quarantine (reason `ocr_garbage`)
    rather than persisting noise that would poison extraction. Applied only
    to image-based documents; a digital text layer is never second-guessed
    here."""
    if not result.is_image_based:
        return None
    ocr_text = "\n".join(p.text for p in result.passages if p.ocr)
    ratio = garbage_ratio(ocr_text)
    if ratio > get_settings().ocr_max_garbage_ratio:
        return f"OCR output garbage ratio {ratio:.2f} exceeds threshold"
    return None


def _run_extraction_stage(db: Session, document: Document) -> ExtractionOutcome | None:
    """B2 extraction over one parsed document, wrapped in an
    extraction_jobs row (§4: status, tier_path, attempts, token/cost
    rollups; the one-live-job partial unique index guards concurrent
    dispatch). NEVER raises, and an LLM failure never quarantines a
    document whose parse succeeded: the job row records the failure and
    the document stays `done` at whatever depth it reached -- degraded,
    logged, recorded, not dropped."""
    settings = get_settings()
    job = ExtractionJob(
        document_id=document.id,
        status="running",
        model=settings.deepseek_model,
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()
    try:
        outcome = run_extraction_sync(
            db,
            document,
            tier1=get_tier1_adapter(settings),
            tier2=get_tier2_adapter(settings),
            settings=settings,
        )
    except Exception as exc:
        db.rollback()  # discard the partial extraction, keep parse+tier-0
        logger.exception("extraction_failed", document_id=str(document.id))
        job = ExtractionJob(
            document_id=document.id,
            status="failed",
            model=settings.deepseek_model,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            error=f"{type(exc).__name__}: {exc}",
        )
        db.add(job)
        db.flush()
        return None
    job.status = "done"
    job.finished_at = datetime.now(timezone.utc)
    job.tier_path = outcome.tier_path
    job.input_tokens = outcome.input_tokens
    job.output_tokens = outcome.output_tokens
    job.cached_input_tokens = outcome.cached_input_tokens
    job.cost_usd = outcome.cost_usd
    db.flush()
    return outcome


def _process_document(document_id: uuid.UUID, extraction_enabled: bool = False) -> _DocumentOutcome:
    """Parse one queued document end to end in its own session. NEVER
    raises: every failure path resolves to a quarantined/failed outcome so
    the coordinator's accounting always receives a terminal answer. Thin
    wrapper around _process_one -- see that function for the actual work;
    this one only owns session lifecycle for the coordinator's thread pool."""
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        if document is None:  # pragma: no cover - coordinator only sends real ids
            logger.error("process_document_missing", document_id=str(document_id))
            return _DocumentOutcome(document_id=document_id, status="failed")
        return _process_one(db, document, extraction_enabled)
    except Exception:
        # Even the failure handling failed (DB down mid-run, ...) -- the
        # outcome stays 'failed'; the coordinator logs and counts it, and
        # the reconciliation gate query will surface the document.
        db.rollback()
        logger.exception("process_document_unrecoverable", document_id=str(document_id))
        return _DocumentOutcome(document_id=document_id, status="failed")
    finally:
        db.close()


def _process_one(
    db: Session, document: Document, extraction_enabled: bool
) -> _DocumentOutcome:
    """Parse (+tier-0, +extraction if enabled) one document against the
    GIVEN session, committing at the same checkpoints _process_document
    always has. NEVER raises internally: every failure path resolves to a
    quarantined outcome, same terminal-status guarantee either caller gets.

    Split out of _process_document so requeue_document() below -- called
    from the exception investigator's resolve_quarantine tool -- can drive
    that exact guarantee inside a session the caller already holds (so an
    uncommitted file_class correction on `document` is visible to this
    reprocessing pass). An investigator resolution must never leave a
    document sitting in a non-terminal status with nothing to pick it back
    up (docs/plan.md §14b SUSPICIOUS 4)."""
    outcome = _DocumentOutcome(document_id=document.id, status="failed")
    try:
        parser_id = classify.PARSER_BY_CLASS[document.file_class]
        content = storage.original_path(
            document.sha256, Path(document.original_filename).suffix
        ).read_bytes()
        result = _run_parser(parser_id, content, document.sha256)

        garbage_detail = _ocr_garbage_detail(result)
        if garbage_detail is not None:
            _quarantine_document(
                db, document, reason_code="ocr_garbage", detail=garbage_detail
            )
            outcome.status = "quarantined"
            db.commit()
            return outcome

        passage_repo = PassageRepository(db)
        persisted = [
            passage_repo.create(
                document_id=document.id,
                seq=seq,
                kind=parsed.kind,
                locator=parsed.locator,
                text=parsed.text,
                ocr=parsed.ocr,
                page_image_sha=parsed.page_image_sha,
            )
            for seq, parsed in enumerate(result.passages)
        ]
        outcome.passages = len(persisted)
        outcome.ocr = any(p.ocr for p in result.passages)

        file_class = None
        if document.file_class == "pdf_digital" and result.is_image_based:
            file_class = "pdf_scanned"
        ProcessingRepository(db).set_document_status(
            document,
            "parsed",
            page_count=result.page_count,
            is_image_based=result.is_image_based,
            file_class=file_class,
        )

        _ingest_attachments(db, document, result, outcome)

        outcome.tier0_elements = _persist_tier0(db, document, persisted)

        if extraction_enabled:
            # Checkpoint parse + tier-0 first: the extraction stage
            # rolls back ITS OWN partial work on failure, and that
            # rollback must never be able to take the parse with it.
            db.commit()
            extraction = _run_extraction_stage(db, document)
            if extraction is not None:
                ProcessingRepository(db).set_document_status(document, "extracted")
                outcome.extracted = True
                outcome.mentions = extraction.mentions
                outcome.llm_elements = extraction.llm_elements
                outcome.tier1_calls = extraction.tier1_calls
                outcome.tier2_calls = extraction.tier2_calls

        ProcessingRepository(db).set_document_status(document, "done")
        outcome.status = "done"
        db.commit()
        return outcome

    except _CORRUPT_EXCEPTIONS as exc:
        db.rollback()
        _quarantine_document(
            db, document, reason_code="corrupt", detail=f"{type(exc).__name__}: {exc}"
        )
        outcome.status = "quarantined"
        db.commit()
        return outcome
    except Exception as exc:
        db.rollback()
        logger.exception(
            "parse_failed", document_id=str(document.id), rel_path=document.rel_path
        )
        _quarantine_document(
            db,
            document,
            reason_code="parser_error",
            detail=f"{type(exc).__name__}: {exc}",
        )
        outcome.status = "quarantined"
        db.commit()
        return outcome


def requeue_document(db: Session, document_id: uuid.UUID) -> _DocumentOutcome:
    """Re-run one document through parse (+tier-0, +extraction when a
    DeepSeek key is configured) against the CALLER's session. The exception
    investigator's resolve_quarantine tool (services/agents/tools.py) calls
    this after applying a corrected file_class or diagnosing a fixable
    parser issue. Synchronous and terminal by construction: by the time
    this returns the document is 'done' or freshly 'quarantined' -- never
    left dangling in a non-terminal status the way a bare
    status='queued' fallback would be, since nothing in this system polls
    for queued documents outside an active processing run."""
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError(f"document {document_id} not found")
    extraction_enabled = bool(get_settings().deepseek_api_key)
    return _process_one(db, document, extraction_enabled)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


async def run_processing_run(run_id: uuid.UUID) -> None:
    """The background task POST /runs dispatches. Owns run status +
    counters; never raises (a run-level failure marks the run `failed`)."""
    settings = get_settings()
    db = SessionLocal()
    try:
        processing = ProcessingRepository(db)
        run = processing.get_run(run_id)
        if run is None:  # pragma: no cover - API creates the row before dispatch
            logger.error("run_missing", run_id=str(run_id))
            return

        processing.mark_run_running(run)
        counters = {key: 0 for key in _COUNTER_KEYS}
        processing.set_counters(run, counters)
        db.commit()
        logger.info("run_started", run_id=str(run_id))

        try:
            corpus_dir = Path(run.config_snapshot["corpus_path"])
            ingest = await asyncio.to_thread(_ingest_corpus, run_id, corpus_dir)
            db.expire(run)  # ingest committed counters from its own session
            counters.update(
                {
                    "files_seen": ingest.files_seen,
                    "ingested": ingest.ingested,
                    "deduped": ingest.deduped,
                    "quarantined": ingest.quarantined,
                }
            )

            semaphore = asyncio.Semaphore(max(1, settings.extraction_max_concurrency))

            # Degrade-gracefully contract (plan §14b / this module's
            # docstring): no DeepSeek key -> no extraction stage, logged
            # exactly ONCE per run, documents finish at tier-0 depth.
            extraction_enabled = bool(settings.deepseek_api_key)
            if not extraction_enabled:
                logger.info(
                    "extraction_skipped",
                    run_id=str(run_id),
                    reason="no_deepseek_api_key",
                )

            async def worker(document_id: uuid.UUID) -> _DocumentOutcome:
                async with semaphore:
                    return await asyncio.to_thread(
                        _process_document, document_id, extraction_enabled
                    )

            pending = list(ingest.queued_ids)
            while pending:
                tasks = [asyncio.create_task(worker(doc_id)) for doc_id in pending]
                pending = []
                for task in asyncio.as_completed(tasks):
                    outcome = await task
                    counters[outcome.status] = counters.get(outcome.status, 0) + 1
                    if outcome.status == "done":
                        counters["parsed"] += 1
                    counters["passages"] += outcome.passages
                    counters["tier0_elements"] += outcome.tier0_elements
                    if outcome.ocr:
                        counters["ocr_documents"] += 1
                    if outcome.extracted:
                        counters["extracted"] += 1
                    counters["mentions"] += outcome.mentions
                    counters["llm_elements"] += outcome.llm_elements
                    counters["tier1_calls"] += outcome.tier1_calls
                    counters["tier2_calls"] += outcome.tier2_calls
                    counters["ingested"] += outcome.child_ingested
                    counters["attachments"] += outcome.child_ingested
                    counters["deduped"] += outcome.child_deduped
                    counters["quarantined"] += outcome.child_quarantined
                    processing.set_counters(run, counters)
                    db.commit()
                    # Children queue behind the current batch -- recursion
                    # terminates because every level ingests new sha256s only.
                    pending.extend(outcome.child_queued_ids)

            processing.mark_run_finished(run, status="finished")
            db.commit()
            logger.info("run_finished", run_id=str(run_id), counters=counters)
        except Exception:
            db.rollback()
            logger.exception("run_failed", run_id=str(run_id))
            processing.mark_run_finished(run, status="failed")
            db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup reaping (UC2 pattern -- wired in main.py's lifespan)
# ---------------------------------------------------------------------------


def reap_stale_jobs() -> int:
    """Mark any non-terminal extraction_jobs row 'failed' (and any orphaned
    'running' processing_runs row likewise) on process startup. A
    non-terminal row can only be left over from a process that died mid-job
    -- never a job this fresh process is already tracking, since it just
    started. Never raises: a DB that is down at startup is the health
    endpoint's problem to report, not a reason to refuse to boot."""
    db = SessionLocal()
    try:
        repo = ProcessingRepository(db)
        jobs = repo.reap_stale_jobs()
        runs = repo.reap_stale_runs()
        db.commit()
        logger.info("reaped_stale_jobs", jobs=jobs, runs=runs)
        return jobs
    except Exception:
        db.rollback()
        logger.exception("reap_stale_jobs_failed")
        return 0
    finally:
        db.close()
