"""The single typed tool registry (docs/plan.md D4: tools are typed Python
functions in ONE registry; native Anthropic tool definitions and the
FastMCP dev server are both facades over this table).

Contract per tool: a Pydantic args model (validated before execution --
docs/plan.md §11), a handler(db, args, ctx) that returns a ToolResult, and
a short model-facing description. Handlers are DB-SCOPED ONLY: every tool
takes ids (document/passage/mention/person/flag/quarantine), never a
filesystem path -- paths are derived server-side from the content-addressed
store, so the model cannot point a tool at arbitrary files (§11: agents
have no filesystem/shell access).

Hard constraints live IN the tools, not in prompts:
- `decide` re-computes the pair's features itself and refuses a merge on
  conflicting strong identifiers or name-only evidence -- is_error result,
  regardless of what the model asked (docs/plan.md D5).
- `decide` gates bulk merges (combined cluster impact > BULK_MERGE_GATE
  active links) behind a human approval_request instead of applying.
- `verify_flag` requires the quoted span to be re-found VERBATIM in one of
  the flag's evidence passages -- structural, not vibes (plan §3's auditor
  row).
- `request_approval` creates approval_requests and returns pending_approval;
  the runner parks the run.

Mutating handlers flush; the surrounding caller commits (the runner's
trace recorder commits the mutation together with the tool-call result row;
the MCP facade commits explicitly). Nothing here logs tool payloads --
evidence text belongs in the DB, not in logs.
"""

from __future__ import annotations

import base64
import io
import json
import string
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping

import pymupdf
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Document,
    ErDecision,
    ExposureFlag,
    FlagEvidence,
    IdentityLink,
    Mention,
    Person,
    PiiElement,
    ProcessingRun,
    Quarantine,
)
from app.repositories.approvals import ApprovalRepository
from app.repositories.er_decisions import ErDecisionRepository
from app.repositories.identity_links import IdentityLinkRepository
from app.repositories.persons import PersonRepository
from app.repositories.review import ReviewRepository
from app.services.er.features import PairFeatures, compute_features
from app.services.er.scoring import ScoredPair, score_pair
from app.services.ingestion import classify
from app.services.ingestion.inventory import sniff_mime
from app.services.parsing import storage
from app.services.parsing.csv_ import parse_csv
from app.services.parsing.docx import parse_docx
from app.services.parsing.eml import parse_eml
from app.services.parsing.html_txt import parse_html, parse_txt
from app.services.parsing.image_ocr import parse_png
from app.services.parsing.images import preprocess as preprocess_image
from app.services.parsing.images import rasterize_pdf_page
from app.services.parsing.ocr_tesseract import run_ocr as tesseract_ocr
from app.services.parsing.pdf import parse_pdf
from app.services.parsing.xlsx import parse_xlsx

logger = get_logger("agents.tools")

# Combined active-link count above which a merge stops being a pairwise
# call and becomes a bulk operation needing human sign-off (docs/plan.md §3:
# "Cluster impact >10 persons -> approval gate").
BULK_MERGE_GATE = 10

_PREVIEW_CHARS = 300
_OCR_TEXT_CHARS = 1200
_PAGE_IMAGE_DPI = 120

ElementType = Literal[
    "ssn",
    "ssn_last4",
    "dob",
    "drivers_license",
    "passport",
    "financial_account",
    "credit_card",
    "medical",
    "credential",
    "address",
    "phone",
    "email",
    "name",
]

ParserId = Literal["pdf", "docx", "xlsx", "csv", "eml", "html", "txt", "png"]

# Classes an investigator may re-route a document to -- exactly the classes
# a deterministic parser exists for (classify.PARSER_BY_CLASS).
ParseableFileClass = Literal[
    "pdf_digital", "pdf_scanned", "docx", "xlsx", "csv", "eml", "html", "txt", "png"
]


# ---------------------------------------------------------------------------
# Result / context / registry plumbing
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    payload: dict
    is_error: bool = False
    # Set by gate tools (request_approval, decide's bulk gate): the runner
    # parks the run as awaiting_approval when it sees this.
    pending_approval_id: uuid.UUID | None = None


@dataclass
class ToolContext:
    """Per-run state the deterministic system reads after the loop: the
    orchestrator's recorded directives, the adjudicator's decision, the
    investigator's resolution/escalation, the auditor's verdicts+estimate.
    MCP calls run with an empty context (no agent run behind them)."""

    agent_run_id: uuid.UUID | None = None
    agent_kind: str | None = None
    trigger: dict = field(default_factory=dict)
    directives: list[dict] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)
    estimate: dict | None = None
    decision: dict | None = None
    resolution: dict | None = None
    escalation: dict | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Session, BaseModel, ToolContext], ToolResult]
    mutating: bool = False


REGISTRY: dict[str, ToolSpec] = {}


def _register(
    name: str,
    description: str,
    args_model: type[BaseModel],
    handler: Callable,
    *,
    mutating: bool = False,
) -> None:
    REGISTRY[name] = ToolSpec(
        name=name,
        description=description,
        args_model=args_model,
        handler=handler,
        mutating=mutating,
    )


def jsonable(obj: object) -> dict:
    """JSONB/tool-result safe: UUIDs, datetimes, Decimals -> strings."""
    return json.loads(json.dumps(obj, default=str))


def _error(message: str, **extra: object) -> ToolResult:
    return ToolResult(payload=jsonable({"error": message, **extra}), is_error=True)


def run_tool(db: Session, name: str, raw_args: dict, ctx: ToolContext) -> ToolResult:
    """Validate args with the tool's Pydantic model, then execute. The one
    entry point both facades use. Unexpected handler exceptions propagate
    -- the runner converts them to a failed run with the trace already
    persisted (trace-before-execute)."""
    spec = REGISTRY.get(name)
    if spec is None:
        return _error(f"unknown tool '{name}'", known_tools=sorted(REGISTRY))
    try:
        args = spec.args_model.model_validate(raw_args)
    except ValidationError as exc:
        return _error(
            "invalid arguments",
            tool=name,
            details=[
                {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]}
                for e in exc.errors()
            ],
        )
    result = spec.handler(db, args, ctx)
    result.payload = jsonable(result.payload)
    return result


def openai_tool_definitions(names: list[str]) -> list[dict]:
    """Facade 1 (docs/plan.md D4, revised 2026-08-12: OpenAI swapped in for
    Anthropic): OpenAI `tools` entries (chat.completions function-calling
    shape) derived from the same Pydantic args models -- no transport
    overhead per turn. `strict` is deliberately omitted, same reasoning as
    extraction/openai_adapter.py's response_format choice: strict mode
    requires transforming the schema into OpenAI's narrower compliant
    subset, unverifiable without a live call and unsafe to guess at."""
    definitions = []
    for name in names:
        spec = REGISTRY[name]
        schema = spec.args_model.model_json_schema()
        schema.setdefault("type", "object")
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            }
        )
    return definitions


# ---------------------------------------------------------------------------
# Shared lookups
# ---------------------------------------------------------------------------


def _document_or_none(db: Session, document_id: uuid.UUID) -> Document | None:
    return db.get(Document, document_id)


def _document_bytes(document: Document) -> bytes:
    from pathlib import Path

    return storage.original_path(
        document.sha256, Path(document.original_filename).suffix
    ).read_bytes()


def _run_named_parser(parser: str, content: bytes, sha256: str):
    if parser == "pdf":
        return parse_pdf(content, sha256=sha256)
    if parser == "png":
        return parse_png(content, sha256=sha256)
    dispatch = {
        "docx": parse_docx,
        "xlsx": parse_xlsx,
        "csv": parse_csv,
        "eml": parse_eml,
        "html": parse_html,
        "txt": parse_txt,
    }
    return dispatch[parser](content)


def _er_mention(db: Session, mention: Mention):
    """Mention row + its attached pii_elements -> the normalized ErMention
    the pure ER functions consume, under the SAME element-admission rule as
    the production stage -- the adjudicator must score with production
    arithmetic over production evidence, and an ungated load would let a
    trap-downgraded or checksum-invalid value fabricate a hard conflict (or
    a merge bridge) the pipeline never saw. Delegates to
    services/er/persist.py's er_mention_from_row so this rule is defined
    exactly once."""
    from app.services.er.persist import er_mention_from_row

    return er_mention_from_row(db, mention)


def _active_link(db: Session, mention_id: uuid.UUID) -> IdentityLink | None:
    return IdentityLinkRepository(db).active_link_for_mention(mention_id)


def _active_links_of_person(db: Session, person_id: uuid.UUID) -> list[IdentityLink]:
    return IdentityLinkRepository(db).active_links_for_person(person_id)


# ---------------------------------------------------------------------------
# Orchestrator tools
# ---------------------------------------------------------------------------


class RunIdArgs(BaseModel):
    run_id: uuid.UUID = Field(description="Processing run id")


def _get_run_stats(db: Session, args: RunIdArgs, ctx: ToolContext) -> ToolResult:
    run = db.get(ProcessingRun, args.run_id)
    if run is None:
        return _error(f"run {args.run_id} not found")
    status_counts = dict(
        db.execute(
            select(Document.status, func.count())
            .where(Document.run_id == run.id)
            .group_by(Document.status)
        ).all()
    )
    open_quarantines = (
        db.scalar(
            select(func.count())
            .select_from(Quarantine)
            .join(Document, Quarantine.document_id == Document.id)
            .where(Document.run_id == run.id, Quarantine.status == "open")
        )
        or 0
    )
    return ToolResult(
        payload={
            "run_id": str(run.id),
            "status": run.status,
            "counters": run.counters,
            "documents_by_status": status_counts,
            "open_quarantines": open_quarantines,
            "started_at": str(run.started_at) if run.started_at else None,
            "finished_at": str(run.finished_at) if run.finished_at else None,
        }
    )


def _get_class_failure_rates(db: Session, args: RunIdArgs, ctx: ToolContext) -> ToolResult:
    if db.get(ProcessingRun, args.run_id) is None:
        return _error(f"run {args.run_id} not found")
    rows = db.execute(
        select(Document.file_class, Document.status, func.count())
        .where(Document.run_id == args.run_id)
        .group_by(Document.file_class, Document.status)
    ).all()
    by_class: dict[str, dict] = {}
    for file_class, doc_status, count in rows:
        entry = by_class.setdefault(
            file_class, {"total": 0, "quarantined": 0, "failed": 0, "done": 0}
        )
        entry["total"] += count
        if doc_status in ("quarantined", "failed", "done"):
            entry[doc_status] += count
    for entry in by_class.values():
        entry["failure_rate"] = round(
            (entry["quarantined"] + entry["failed"]) / entry["total"], 4
        )
    return ToolResult(payload={"run_id": str(args.run_id), "classes": by_class})


class ListQuarantinesArgs(BaseModel):
    run_id: uuid.UUID | None = Field(default=None, description="Restrict to one run")
    status: Literal["open", "agent_resolved", "escalated", "human_resolved"] = "open"
    limit: int = Field(default=20, ge=1, le=50)


def _list_quarantines(db: Session, args: ListQuarantinesArgs, ctx: ToolContext) -> ToolResult:
    query = (
        select(Quarantine, Document)
        .join(Document, Quarantine.document_id == Document.id)
        .where(Quarantine.status == args.status)
        .order_by(Quarantine.created_at)
        .limit(args.limit)
    )
    if args.run_id is not None:
        query = query.where(Document.run_id == args.run_id)
    rows = db.execute(query).all()
    return ToolResult(
        payload={
            "quarantines": [
                {
                    "quarantine_id": str(q.id),
                    "document_id": str(d.id),
                    "filename": d.original_filename,
                    "rel_path": d.rel_path,
                    "file_class": d.file_class,
                    "stage": q.stage,
                    "reason_code": q.reason_code,
                    "detail": q.detail,
                }
                for q, d in rows
            ]
        }
    )


class SetBatchPriorityArgs(BaseModel):
    file_class: ParseableFileClass
    priority: int = Field(ge=0, le=10, description="0 = lowest, 10 = process first")
    reason: str = Field(min_length=1, max_length=500)


def _set_batch_priority(db: Session, args: SetBatchPriorityArgs, ctx: ToolContext) -> ToolResult:
    directive = {"action": "set_batch_priority", "params": args.model_dump(mode="json")}
    ctx.directives.append(directive)
    return ToolResult(payload={"recorded": True, "directive": directive})


class SetEscalationThresholdArgs(BaseModel):
    threshold: float = Field(
        ge=0.0, le=1.0, description="Composite-confidence theta below which tier 1 escalates"
    )
    reason: str = Field(min_length=1, max_length=500)


def _set_escalation_threshold(
    db: Session, args: SetEscalationThresholdArgs, ctx: ToolContext
) -> ToolResult:
    directive = {
        "action": "set_escalation_threshold",
        "params": args.model_dump(mode="json"),
    }
    ctx.directives.append(directive)
    return ToolResult(payload={"recorded": True, "directive": directive})


class DispatchInvestigatorArgs(BaseModel):
    quarantine_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)


def _dispatch_investigator(
    db: Session, args: DispatchInvestigatorArgs, ctx: ToolContext
) -> ToolResult:
    quarantine = db.get(Quarantine, args.quarantine_id)
    if quarantine is None:
        return _error(f"quarantine {args.quarantine_id} not found")
    if quarantine.status != "open":
        return _error(
            f"quarantine {args.quarantine_id} is '{quarantine.status}', not open"
        )
    directive = {
        "action": "dispatch_investigator",
        "params": args.model_dump(mode="json"),
    }
    ctx.directives.append(directive)
    return ToolResult(payload={"recorded": True, "directive": directive})


class RequestApprovalArgs(BaseModel):
    action_type: Literal["bulk_merge", "final_signoff", "class_pause"]
    payload: dict = Field(default_factory=dict, description="What is being approved")


def _request_approval(db: Session, args: RequestApprovalArgs, ctx: ToolContext) -> ToolResult:
    if ctx.agent_run_id is None:
        return _error("request_approval requires an agent-run context")
    approval = ApprovalRepository(db).create(
        agent_run_id=ctx.agent_run_id,
        action_type=args.action_type,
        payload=jsonable(args.payload),
    )
    return ToolResult(
        payload={
            "status": "pending_approval",
            "approval_request_id": str(approval.id),
            "action_type": args.action_type,
        },
        pending_approval_id=approval.id,
    )


# ---------------------------------------------------------------------------
# Investigator tools
# ---------------------------------------------------------------------------


class DocumentIdArgs(BaseModel):
    document_id: uuid.UUID


def _get_document_meta(db: Session, args: DocumentIdArgs, ctx: ToolContext) -> ToolResult:
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    quarantines = (
        db.execute(select(Quarantine).where(Quarantine.document_id == document.id))
        .scalars()
        .all()
    )
    return ToolResult(
        payload={
            "document_id": str(document.id),
            "filename": document.original_filename,
            "rel_path": document.rel_path,
            "declared_mime": document.declared_mime,
            "sniffed_mime": document.sniffed_mime,
            "file_class": document.file_class,
            "status": document.status,
            "byte_size": document.byte_size,
            "page_count": document.page_count,
            "is_image_based": document.is_image_based,
            "source_kind": document.source_kind,
            "parent_document_id": (
                str(document.parent_document_id) if document.parent_document_id else None
            ),
            "quarantines": [
                {
                    "quarantine_id": str(q.id),
                    "stage": q.stage,
                    "reason_code": q.reason_code,
                    "detail": q.detail,
                    "status": q.status,
                }
                for q in quarantines
            ],
        }
    )


class RawBytesArgs(BaseModel):
    document_id: uuid.UUID
    num_bytes: int = Field(default=64, ge=1, le=512)


def _get_raw_bytes_head(db: Session, args: RawBytesArgs, ctx: ToolContext) -> ToolResult:
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    head = _document_bytes(document)[: args.num_bytes]
    printable = "".join(
        ch if ch in string.printable and ch not in "\r\x0b\x0c" else "."
        for ch in head.decode("latin-1")
    )
    return ToolResult(
        payload={
            "document_id": str(document.id),
            "num_bytes": len(head),
            "hex": head.hex(" "),
            "printable": printable,
        }
    )


def _sniff_type(db: Session, args: DocumentIdArgs, ctx: ToolContext) -> ToolResult:
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    content = _document_bytes(document)
    sniffed = sniff_mime(content)
    decision = classify.route(
        filename=document.original_filename,
        byte_size=len(content),
        declared_mime=document.declared_mime,
        sniffed_mime=sniffed,
        content=content,
    )
    return ToolResult(
        payload={
            "document_id": str(document.id),
            "declared_mime": document.declared_mime,
            "sniffed_mime": sniffed,
            "recorded_file_class": document.file_class,
            "suggested_file_class": decision.file_class,
            "would_quarantine": decision.quarantine_reason,
            "detail": decision.detail,
            "reclassified": decision.reclassified,
        }
    )


class TryParserArgs(BaseModel):
    document_id: uuid.UUID
    parser: ParserId


def _try_parser(db: Session, args: TryParserArgs, ctx: ToolContext) -> ToolResult:
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    content = _document_bytes(document)
    try:
        result = _run_named_parser(args.parser, content, document.sha256)
    except Exception as exc:  # a failed parse IS the diagnostic signal
        return _error(
            "parser failed",
            parser=args.parser,
            exception=type(exc).__name__,
            detail=str(exc)[:500],
        )
    first_text = result.passages[0].text if result.passages else ""
    return ToolResult(
        payload={
            "parser": args.parser,
            "ok": True,
            "passage_count": len(result.passages),
            "page_count": result.page_count,
            "is_image_based": result.is_image_based,
            "attachment_count": len(result.attachments),
            "first_passage_preview": first_text[:_PREVIEW_CHARS],
        }
    )


class RunOcrArgs(BaseModel):
    document_id: uuid.UUID
    page: int = Field(default=0, ge=0, description="0-based page index")
    dpi: int = Field(default=300, ge=72, le=400)
    preprocess: bool = Field(default=True, description="Deskew + denoise before OCR")


def _run_ocr(db: Session, args: RunOcrArgs, ctx: ToolContext) -> ToolResult:
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    content = _document_bytes(document)
    deskew_angle = None
    try:
        if document.file_class in ("pdf_digital", "pdf_scanned"):
            pdf = pymupdf.open(stream=content, filetype="pdf")
            try:
                if args.page >= pdf.page_count:
                    return _error(
                        f"page {args.page} out of range (document has {pdf.page_count} pages)"
                    )
                image = rasterize_pdf_page(pdf[args.page], args.dpi)
            finally:
                pdf.close()
        elif document.file_class == "png":
            if args.page != 0:
                return _error("PNG documents have a single page (use page=0)")
            image = Image.open(io.BytesIO(content)).convert("RGB")
        else:
            return _error(
                f"run_ocr supports pdf/png documents, not file_class '{document.file_class}'"
            )
    except Exception as exc:
        # A file too malformed to even open/render for OCR IS the diagnostic
        # signal (same posture _try_parser already takes on a failed parse,
        # docs/plan.md agent-layer investigation: this is a genuine
        # investigator-agent tool result the model reasons over next --
        # e.g. "OCR failed too, this looks unrecoverable, escalate" -- not
        # an implementation bug that should crash the whole agent run.
        # Found live 2026-08-13: a genuinely truncated corpus PDF crashed
        # this exact path with an unhandled pymupdf.FileDataError before
        # this guard existed.
        return _error(
            "failed to open/render document for OCR",
            exception=type(exc).__name__,
            detail=str(exc)[:500],
        )
    if args.preprocess:
        pre = preprocess_image(image)
        image = pre.image
        deskew_angle = pre.deskew_angle
    page_text, mean_conf = tesseract_ocr(image)
    return ToolResult(
        payload={
            "document_id": str(document.id),
            "page": args.page,
            "dpi": args.dpi,
            "preprocessed": args.preprocess,
            "deskew_angle": deskew_angle,
            "mean_word_confidence": round(mean_conf, 2),
            "word_count": len(page_text.words),
            "text": page_text.text[:_OCR_TEXT_CHARS],
        }
    )


class PageImageArgs(BaseModel):
    document_id: uuid.UUID
    page: int = Field(default=0, ge=0)


def _get_page_image(db: Session, args: PageImageArgs, ctx: ToolContext) -> ToolResult:
    """Returns the rendered page as base64 under `_image_base64` -- the
    model-client adapter turns it into a vision content block (the tier-2
    'look at the pixels' path for OCR-hostile pages)."""
    document = _document_or_none(db, args.document_id)
    if document is None:
        return _error(f"document {args.document_id} not found")
    content = _document_bytes(document)
    try:
        if document.file_class in ("pdf_digital", "pdf_scanned"):
            pdf = pymupdf.open(stream=content, filetype="pdf")
            try:
                if args.page >= pdf.page_count:
                    return _error(
                        f"page {args.page} out of range (document has {pdf.page_count} pages)"
                    )
                image = rasterize_pdf_page(pdf[args.page], _PAGE_IMAGE_DPI)
            finally:
                pdf.close()
        elif document.file_class == "png":
            if args.page != 0:
                return _error("PNG documents have a single page (use page=0)")
            image = Image.open(io.BytesIO(content)).convert("RGB")
        else:
            return _error(
                f"get_page_image supports pdf/png documents, not '{document.file_class}'"
            )
    except Exception as exc:
        # Same guard and same reasoning as _run_ocr just above -- a
        # malformed file failing to open/render is a diagnostic signal for
        # the calling agent, not a process-crashing implementation bug.
        return _error(
            "failed to open/render document for get_page_image",
            exception=type(exc).__name__,
            detail=str(exc)[:500],
        )
    image.thumbnail((1600, 1600))  # bound payload size; plenty for reading a page
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return ToolResult(
        payload={
            "document_id": str(document.id),
            "page": args.page,
            "media_type": "image/png",
            "width": image.width,
            "height": image.height,
            "_image_base64": base64.standard_b64encode(buffer.getvalue()).decode("ascii"),
        }
    )


class ResolveQuarantineArgs(BaseModel):
    quarantine_id: uuid.UUID
    resolution: str = Field(min_length=5, max_length=1000, description="What was wrong and the fix")
    corrected_file_class: ParseableFileClass | None = Field(
        default=None, description="Re-route the document to this class before re-parsing"
    )


def _resolve_quarantine(db: Session, args: ResolveQuarantineArgs, ctx: ToolContext) -> ToolResult:
    quarantine = db.get(Quarantine, args.quarantine_id)
    if quarantine is None:
        return _error(f"quarantine {args.quarantine_id} not found")
    if quarantine.status != "open":
        return _error(
            f"quarantine {args.quarantine_id} is '{quarantine.status}', not open"
        )
    document = db.get(Document, quarantine.document_id)
    quarantine.status = "agent_resolved"
    quarantine.resolved_by_agent_run_id = ctx.agent_run_id
    quarantine.resolution = {
        "resolution": args.resolution,
        "corrected_file_class": args.corrected_file_class,
    }
    if args.corrected_file_class is not None:
        document.file_class = args.corrected_file_class
    db.flush()  # correction visible to the reprocessing pass below
    reprocessed_status = _requeue_document(db, document)
    ctx.resolution = {
        "quarantine_id": str(quarantine.id),
        "document_id": str(document.id),
        "resolution": args.resolution,
        "corrected_file_class": args.corrected_file_class,
        "reprocessed_status": reprocessed_status,
    }
    return ToolResult(payload=dict(ctx.resolution, status="agent_resolved"))


def _requeue_document(db: Session, document: Document) -> str:
    """Re-run the document through the pipeline's requeue_document() in
    THIS session (docs/plan.md §14b SUSPICIOUS 4): synchronous and
    terminal by construction, so a resolved quarantine never leaves a
    document dangling in a non-terminal status with nothing to pick it
    back up. Returns the outcome status ('done' or 'quarantined' again --
    the investigator's own resolution can itself fail to fix the file,
    which is a legitimate outcome, not a tool error)."""
    from app.services.pipeline import requeue_document

    outcome = requeue_document(db, document.id)
    return outcome.status


class EscalateArgs(BaseModel):
    reason: str = Field(min_length=5, max_length=500)
    diagnosis: str = Field(
        min_length=5, max_length=2000, description="Structured findings so far"
    )


def _escalate(db: Session, args: EscalateArgs, ctx: ToolContext) -> ToolResult:
    quarantine_id = ctx.trigger.get("quarantine_id")
    if quarantine_id:
        quarantine = db.get(Quarantine, uuid.UUID(str(quarantine_id)))
        if quarantine is not None and quarantine.status == "open":
            quarantine.status = "escalated"
            quarantine.resolved_by_agent_run_id = ctx.agent_run_id
            quarantine.resolution = {
                "escalated": True,
                "reason": args.reason,
                "diagnosis": args.diagnosis,
            }
            db.flush()
    ctx.escalation = {
        "reason": args.reason,
        "diagnosis": args.diagnosis,
        "quarantine_id": str(quarantine_id) if quarantine_id else None,
    }
    return ToolResult(payload=dict(ctx.escalation, status="escalated"))


# ---------------------------------------------------------------------------
# Adjudicator tools
# ---------------------------------------------------------------------------


class MentionIdArgs(BaseModel):
    mention_id: uuid.UUID


def _get_mention_evidence(db: Session, args: MentionIdArgs, ctx: ToolContext) -> ToolResult:
    mention = db.get(Mention, args.mention_id)
    if mention is None:
        return _error(f"mention {args.mention_id} not found")
    document = db.get(Document, mention.document_id)
    from app.db.models import Passage

    passage = db.get(Passage, mention.passage_id)
    elements = (
        db.execute(select(PiiElement).where(PiiElement.mention_id == mention.id))
        .scalars()
        .all()
    )
    link = _active_link(db, mention.id)
    return ToolResult(
        payload={
            "mention_id": str(mention.id),
            "name_raw": mention.name_raw,
            "name_normalized": mention.name_normalized,
            "dob": mention.dob.isoformat() if mention.dob else None,
            "detector": mention.detector,
            "confidence": float(mention.confidence) if mention.confidence else None,
            "document": {
                "document_id": str(document.id),
                "rel_path": document.rel_path,
                "file_class": document.file_class,
            },
            "passage_preview": (passage.text[:_PREVIEW_CHARS] if passage else None),
            "linked_person_id": str(link.person_id) if link else None,
            "elements": [
                {
                    "element_type": e.element_type,
                    "value_normalized": e.value_normalized,
                    "validation_status": e.validation_status,
                    "detector": e.detector,
                }
                for e in elements
            ],
        }
    )


class PersonIdArgs(BaseModel):
    person_id: uuid.UUID


def _get_person_summary(db: Session, args: PersonIdArgs, ctx: ToolContext) -> ToolResult:
    person = db.get(Person, args.person_id)
    if person is None:
        return _error(f"person {args.person_id} not found")
    links = _active_links_of_person(db, person.id)
    mention_ids = [link.mention_id for link in links]
    doc_count = 0
    if mention_ids:
        doc_count = (
            db.scalar(
                select(func.count(func.distinct(Mention.document_id))).where(
                    Mention.id.in_(mention_ids)
                )
            )
            or 0
        )
    flags = (
        db.execute(select(ExposureFlag).where(ExposureFlag.person_id == person.id))
        .scalars()
        .all()
    )
    return ToolResult(
        payload={
            "person_id": str(person.id),
            "best_name": person.best_name,
            "aliases": person.aliases or [],
            "dob": person.dob.isoformat() if person.dob else None,
            "er_confidence": float(person.er_confidence) if person.er_confidence else None,
            "review_status": person.review_status,
            "active_mention_count": len(links),
            "document_count": doc_count,
            "flags": [
                {
                    "category": f.category,
                    "exposed": f.exposed,
                    "confidence": float(f.confidence) if f.confidence else None,
                }
                for f in flags
            ],
        }
    )


class ComparePairArgs(BaseModel):
    left_mention_id: uuid.UUID
    right_mention_id: uuid.UUID


def _compare_features(db: Session, args: ComparePairArgs, ctx: ToolContext) -> ToolResult:
    if args.left_mention_id == args.right_mention_id:
        return _error("left and right mention ids are identical")
    left = db.get(Mention, args.left_mention_id)
    right = db.get(Mention, args.right_mention_id)
    if left is None or right is None:
        return _error("one or both mentions not found")
    features = compute_features(_er_mention(db, left), _er_mention(db, right))
    scored = score_pair(str(left.id), str(right.id), features)
    return ToolResult(
        payload={
            "left_mention_id": str(left.id),
            "right_mention_id": str(right.id),
            "score": scored.score,
            "hard_reason": scored.hard_reason,
            "features": scored.features,
        }
    )


class SearchElementsArgs(BaseModel):
    element_type: ElementType
    value_normalized: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=50)


def _search_elements(db: Session, args: SearchElementsArgs, ctx: ToolContext) -> ToolResult:
    rows = (
        db.execute(
            select(PiiElement)
            .where(
                PiiElement.element_type == args.element_type,
                PiiElement.value_normalized == args.value_normalized,
            )
            .limit(args.limit)
        )
        .scalars()
        .all()
    )
    return ToolResult(
        payload={
            "element_type": args.element_type,
            "match_count": len(rows),
            "matches": [
                {
                    "pii_element_id": str(e.id),
                    "document_id": str(e.document_id),
                    "passage_id": str(e.passage_id),
                    "mention_id": str(e.mention_id) if e.mention_id else None,
                    "validation_status": e.validation_status,
                    "detector": e.detector,
                }
                for e in rows
            ],
        }
    )


class DecideArgs(BaseModel):
    left_mention_id: uuid.UUID
    right_mention_id: uuid.UUID
    decision: Literal["merge", "no_merge", "escalate"]
    rationale: str = Field(
        min_length=10, max_length=2000, description="Cite the features that decided it"
    )


def _decide(db: Session, args: DecideArgs, ctx: ToolContext) -> ToolResult:
    """The adjudicator's single write path. THE TOOL enforces D5's hard
    constraints -- a merge on conflicting strong identifiers or bare
    name-only evidence is refused with is_error, regardless of what the
    model asked; bulk-impact merges stop at a human gate."""
    if args.left_mention_id == args.right_mention_id:
        return _error("left and right mention ids are identical")
    left = db.get(Mention, args.left_mention_id)
    right = db.get(Mention, args.right_mention_id)
    if left is None or right is None:
        return _error("one or both mentions not found")
    features = compute_features(_er_mention(db, left), _er_mention(db, right))
    scored = score_pair(str(left.id), str(right.id), features)

    if args.decision == "merge":
        if features.conflicting_strong_types:
            return _error(
                "merge refused: conflicting strong identifiers "
                f"({', '.join(features.conflicting_strong_types)}) -- two people who "
                "carry different values of the same strong identifier are never the "
                "same person, regardless of name similarity. Record no_merge or "
                "escalate instead.",
                hard_constraint="hard_conflict",
                conflicting_types=list(features.conflicting_strong_types),
            )
        if not features.has_corroboration:
            return _error(
                "merge refused: name-only evidence -- no shared identifier, DOB "
                "match, last-4 bridge, address overlap, or same-document "
                "co-occurrence corroborates this pair. A merge on name similarity "
                "alone is forbidden (SharedName protection). Record no_merge or "
                "escalate instead.",
                hard_constraint="name_only",
            )
        impact = _merge_impact(db, left, right)
        if impact > BULK_MERGE_GATE:
            if ctx.agent_run_id is None:
                return _error(
                    f"merge affects {impact} linked mentions (> {BULK_MERGE_GATE}) and "
                    "requires a human approval gate, which needs an agent-run context"
                )
            approval = ApprovalRepository(db).create(
                agent_run_id=ctx.agent_run_id,
                action_type="bulk_merge",
                payload=jsonable(
                    {
                        "left_mention_id": str(left.id),
                        "right_mention_id": str(right.id),
                        "rationale": args.rationale,
                        "score": scored.score,
                        "impact_mentions": impact,
                    }
                ),
            )
            return ToolResult(
                payload={
                    "status": "pending_approval",
                    "approval_request_id": str(approval.id),
                    "impact_mentions": impact,
                },
                pending_approval_id=approval.id,
            )
        outcome = _perform_merge(
            db, ctx, left, right, features, scored, args.rationale, approval_id=None
        )
        ctx.decision = outcome
        return ToolResult(payload=outcome)

    decision_row = _record_er_decision(
        db, ctx, left, right, scored, args.decision, args.rationale, approval_id=None
    )
    outcome = {
        "decision": args.decision,
        "er_decision_id": str(decision_row.id),
        "score": scored.score,
    }
    # docs/plan.md §14c's already-fixed "wasted adjudicator budget on an
    # already-settled pair" gap, reopened for decide's OTHER two outcomes
    # (close_superseded_er_pairs only ever closes via same_cluster/
    # hard_conflict side effects that a successful merge happens to
    # produce -- neither no_merge nor escalate produces either one). Local
    # import: same deferred-cross-service-import convention this module
    # already uses for services.er.persist elsewhere (_recompute_exposure_
    # and_supersede above).
    from app.services.er.persist import close_decided_er_pair_item, find_open_er_pair_item

    run_id = db.get(Document, left.document_id).run_id
    if args.decision == "no_merge":
        closed_item_id = close_decided_er_pair_item(
            db, run_id, left.id, right.id, decision=args.decision, rationale=args.rationale
        )
        if closed_item_id is not None:
            outcome["closed_review_item_id"] = str(closed_item_id)
    elif args.decision == "escalate":
        existing = find_open_er_pair_item(db, run_id, left.id, right.id)
        if existing is not None:
            # Already queued (e.g. the ER stage's own gray-band item this
            # pair came from) -- reuse it rather than creating a duplicate
            # the original stayed open and un-cross-referenced forever.
            outcome["review_item_id"] = str(existing.id)
            outcome["review_item_reused"] = True
        else:
            item = ReviewRepository(db).create_item(
                kind="er_pair",
                ref={
                    "run_id": str(run_id),  # was missing: made the item
                    # invisible to every run-scoped GET /review/items?
                    # run_id=... query, including find_open_er_pair_item's
                    # own lookup above on a future re-dispatch.
                    "left_mention_id": str(left.id),
                    "right_mention_id": str(right.id),
                    "er_decision_id": str(decision_row.id),
                },
                reason=args.rationale,
                priority=1,
            )
            outcome["review_item_id"] = str(item.id)
    ctx.decision = outcome
    return ToolResult(payload=outcome)


def _merge_impact(db: Session, left: Mention, right: Mention) -> int:
    """Active links across both sides' clusters (an unlinked mention counts
    as itself) -- the bulk_merge gate's 'cluster impact' number."""
    total = 0
    seen: set[uuid.UUID] = set()
    for mention in (left, right):
        link = _active_link(db, mention.id)
        if link is None:
            total += 1
        elif link.person_id not in seen:
            seen.add(link.person_id)
            total += len(_active_links_of_person(db, link.person_id))
    return total


def _alias_kind(features: PairFeatures) -> str:
    if features.nickname_hit:
        return "nickname"
    if features.initials_compatible:
        return "initials"
    if features.surname_change_candidate:
        return "maiden"
    if features.surname_exact and features.given_exact:
        return "order_variant"
    return "misspelling"


def _record_er_decision(
    db: Session,
    ctx: ToolContext,
    left: Mention,
    right: Mention,
    scored: ScoredPair,
    decision: str,
    rationale: str,
    *,
    approval_id: uuid.UUID | None,
    method: str = "agent",
) -> ErDecision:
    return ErDecisionRepository(db).create(
        left_ref={"mention_id": str(left.id)},
        right_ref={"mention_id": str(right.id)},
        decision=decision,
        score=scored.score,
        features=scored.features,
        method=method,
        rationale=rationale,
        agent_run_id=ctx.agent_run_id,
        approval_request_id=approval_id,
    )


def _recompute_exposure_and_supersede(
    db: Session, run_id: uuid.UUID, person_ids: set[uuid.UUID]
) -> None:
    """After a successful agent-driven merge (docs/plan.md §14c SUSPICIOUS
    5b): recompute flags/evidence for every affected person immediately --
    services/review.py's reviewer merge path already does this
    (recompute_for_persons) after every merge/split; the adjudicator's own
    write path skipped it, leaving flags/evidence/doc-counts stale until the
    next manual compute_exposure call. Also closes any other now-moot
    gray-band review items this merge answered (SUSPICIOUS 5a) so the
    adjudicator never re-spends live budget on an already-settled pair.
    Local import: same deferred-cross-service-import convention this module
    already uses for services.er.persist / services.pipeline."""
    from app.services.er.persist import close_superseded_er_pairs
    from app.services.exposure import recompute_for_persons

    if person_ids:
        recompute_for_persons(db, run_id, sorted(person_ids))
    close_superseded_er_pairs(db, run_id)


def _perform_merge(
    db: Session,
    ctx: ToolContext,
    left: Mention,
    right: Mention,
    features: PairFeatures,
    scored: ScoredPair,
    rationale: str,
    *,
    approval_id: uuid.UUID | None,
) -> dict:
    """Apply an (already constraint-checked) merge through the er persist
    interfaces: append-only identity_links (old active rows flipped, new
    rows written -- never deleted) + an er_decisions row. Recomputes
    exposure for every affected person and closes now-moot gray-band items
    before returning (docs/plan.md §14c SUSPICIOUS 5a/5b) -- see
    _recompute_exposure_and_supersede above. Returns the outcome dict the
    adjudicator's contract reports."""
    run_id = db.get(Document, left.document_id).run_id
    link_left = _active_link(db, left.id)
    link_right = _active_link(db, right.id)

    if link_left is not None and link_right is not None and link_left.person_id == link_right.person_id:
        decision_row = _record_er_decision(
            db, ctx, left, right, scored, "merge", rationale, approval_id=approval_id
        )
        _recompute_exposure_and_supersede(db, run_id, {link_left.person_id})
        return {
            "decision": "merge",
            "already_linked": True,
            "person_id": str(link_left.person_id),
            "er_decision_id": str(decision_row.id),
            "score": scored.score,
        }

    links = IdentityLinkRepository(db)
    if link_left is not None:
        target = db.get(Person, link_left.person_id)
    elif link_right is not None:
        target = db.get(Person, link_right.person_id)
    else:
        document = db.get(Document, left.document_id)
        target = PersonRepository(db).create(
            run_id=document.run_id, best_name=left.name_raw
        )
        _add_link(db, ctx, target, left, scored.score, rationale)

    moved = 0
    absorbed_person_ids: set[uuid.UUID] = set()
    for mention, link in ((left, link_left), (right, link_right)):
        if link is None:
            if _active_link(db, mention.id) is None:  # not just created above
                _add_link(db, ctx, target, mention, scored.score, rationale)
                moved += 1
        elif link.person_id != target.id:
            source = db.get(Person, link.person_id)
            absorbed_person_ids.add(source.id)
            for source_link in _active_links_of_person(db, source.id):
                links.deactivate(source_link)  # append-only: flip, never delete
                _add_link(
                    db,
                    ctx,
                    target,
                    db.get(Mention, source_link.mention_id),
                    scored.score,
                    rationale,
                )
                moved += 1
            alias_entry = {"name": source.best_name, "kind": _alias_kind(features)}
            aliases = list(target.aliases or [])
            if source.best_name != target.best_name and alias_entry not in aliases:
                aliases.append(alias_entry)
                target.aliases = aliases
            if target.dob is None and source.dob is not None:
                target.dob = source.dob

    _refresh_person_counts(db, target)
    for absorbed_id in absorbed_person_ids:
        # Mirrors services/review.py's _merge_persons, which refreshes BOTH
        # sides: an absorbed person's active links just dropped to zero and
        # its mention/document counts must reflect that immediately, not
        # only the survivor's.
        _refresh_person_counts(db, db.get(Person, absorbed_id))
    if target.er_confidence is None or scored.score < float(target.er_confidence):
        target.er_confidence = scored.score
    db.flush()
    decision_row = _record_er_decision(
        db, ctx, left, right, scored, "merge", rationale, approval_id=approval_id
    )
    _recompute_exposure_and_supersede(db, run_id, {target.id, *absorbed_person_ids})
    return {
        "decision": "merge",
        "person_id": str(target.id),
        "mentions_moved": moved,
        "er_decision_id": str(decision_row.id),
        "score": scored.score,
    }


def _add_link(
    db: Session,
    ctx: ToolContext,
    person: Person,
    mention: Mention,
    score: float,
    rationale: str,
) -> None:
    IdentityLinkRepository(db).create(
        person_id=person.id,
        mention_id=mention.id,
        score=score,
        method="agent",
        rule_id="adjudicator_merge",
        rationale=rationale,
        agent_run_id=ctx.agent_run_id,
    )


def _refresh_person_counts(db: Session, person: Person) -> None:
    links = _active_links_of_person(db, person.id)
    person.mention_count = len(links)
    mention_ids = [link.mention_id for link in links]
    person.document_count = (
        (
            db.scalar(
                select(func.count(func.distinct(Mention.document_id))).where(
                    Mention.id.in_(mention_ids)
                )
            )
            or 0
        )
        if mention_ids
        else 0
    )


def apply_approved_action(db: Session, approval, ctx: ToolContext) -> dict:
    """The deterministic half of a human decision (docs/plan.md §3: agents
    emit decisions, the deterministic system applies them). Called by the
    runner when a parked run resumes."""
    if approval.status == "rejected":
        if approval.action_type == "bulk_merge":
            left = db.get(Mention, uuid.UUID(approval.payload["left_mention_id"]))
            right = db.get(Mention, uuid.UUID(approval.payload["right_mention_id"]))
            if left is not None and right is not None:
                scored = score_pair(
                    str(left.id),
                    str(right.id),
                    compute_features(_er_mention(db, left), _er_mention(db, right)),
                )
                row = _record_er_decision(
                    db,
                    ctx,
                    left,
                    right,
                    scored,
                    "no_merge",
                    f"bulk merge rejected by {approval.decided_by}",
                    approval_id=approval.id,
                    method="reviewer",
                )
                db.flush()
                return {"approval": "rejected", "er_decision_id": str(row.id)}
        return {"approval": "rejected"}

    if approval.action_type == "bulk_merge":
        left = db.get(Mention, uuid.UUID(approval.payload["left_mention_id"]))
        right = db.get(Mention, uuid.UUID(approval.payload["right_mention_id"]))
        if left is None or right is None:
            return {"approval": "approved", "applied": False, "error": "mentions vanished"}
        features = compute_features(_er_mention(db, left), _er_mention(db, right))
        if features.conflicting_strong_types:  # constraints hold even post-approval
            return {
                "approval": "approved",
                "applied": False,
                "error": "hard_conflict appeared since the gate -- merge still refused",
            }
        scored = score_pair(str(left.id), str(right.id), features)
        outcome = _perform_merge(
            db,
            ctx,
            left,
            right,
            features,
            scored,
            approval.payload.get("rationale", "approved bulk merge"),
            approval_id=approval.id,
        )
        db.flush()
        return {"approval": "approved", "applied": True, **outcome}

    return {"approval": "approved", "applied": True, "action_type": approval.action_type}


# ---------------------------------------------------------------------------
# Auditor tools
# ---------------------------------------------------------------------------


class FlagIdArgs(BaseModel):
    flag_id: uuid.UUID


def _get_flag_with_evidence(db: Session, args: FlagIdArgs, ctx: ToolContext) -> ToolResult:
    flag = db.get(ExposureFlag, args.flag_id)
    if flag is None:
        return _error(f"exposure flag {args.flag_id} not found")
    person = db.get(Person, flag.person_id)
    evidence = (
        db.execute(
            select(FlagEvidence, PiiElement, Document)
            .join(PiiElement, FlagEvidence.pii_element_id == PiiElement.id)
            .join(Document, FlagEvidence.document_id == Document.id)
            .where(FlagEvidence.exposure_flag_id == flag.id)
        )
        .all()
    )
    return ToolResult(
        payload={
            "flag_id": str(flag.id),
            "category": flag.category,
            "exposed": flag.exposed,
            "confidence": float(flag.confidence) if flag.confidence else None,
            "review_status": flag.review_status,
            "person": {"person_id": str(person.id), "best_name": person.best_name},
            "evidence": [
                {
                    "passage_id": str(ev.passage_id),
                    "document_id": str(doc.id),
                    "rel_path": doc.rel_path,
                    "snippet": ev.snippet,
                    "element_type": element.element_type,
                    "value_normalized": element.value_normalized,
                    "validation_status": element.validation_status,
                }
                for ev, element, doc in evidence
            ],
        }
    )


class PassageTextArgs(BaseModel):
    passage_id: uuid.UUID
    max_chars: int = Field(default=4000, ge=100, le=8000)


def _get_passage_text(db: Session, args: PassageTextArgs, ctx: ToolContext) -> ToolResult:
    from app.db.models import Passage

    passage = db.get(Passage, args.passage_id)
    if passage is None:
        return _error(f"passage {args.passage_id} not found")
    return ToolResult(
        payload={
            "passage_id": str(passage.id),
            "document_id": str(passage.document_id),
            "kind": passage.kind,
            "locator": passage.locator,
            "ocr": passage.ocr,
            "char_length": len(passage.text),
            "text": passage.text[: args.max_chars],
        }
    )


class VerifyFlagArgs(BaseModel):
    flag_id: uuid.UUID
    verdict: Literal["verified", "contradicted"]
    quoted_span: str = Field(
        min_length=3,
        max_length=500,
        description="Exact text copied from the evidence passage that grounds the verdict",
    )
    notes: str | None = Field(default=None, max_length=1000)


def _verify_flag(db: Session, args: VerifyFlagArgs, ctx: ToolContext) -> ToolResult:
    """A verdict is only accepted when its quote re-finds VERBATIM in one of
    the flag's evidence passages -- the auditor cannot assert what it cannot
    point at (structural check, docs/plan.md §3)."""
    flag = db.get(ExposureFlag, args.flag_id)
    if flag is None:
        return _error(f"exposure flag {args.flag_id} not found")
    from app.db.models import Passage

    passage_ids = (
        db.execute(
            select(FlagEvidence.passage_id).where(FlagEvidence.exposure_flag_id == flag.id)
        )
        .scalars()
        .all()
    )
    if not passage_ids:
        return _error(
            f"flag {flag.id} has no evidence rows -- that itself is a contradiction; "
            "report it via notes on a contradicted verdict is impossible, escalate "
            "to review instead"
        )
    matched_passage: uuid.UUID | None = None
    for pid in passage_ids:
        passage = db.get(Passage, pid)
        if passage is not None and args.quoted_span in passage.text:
            matched_passage = pid
            break
    if matched_passage is None:
        return _error(
            "quoted_span was not found verbatim in any evidence passage for this "
            "flag -- re-read the passage with get_passage_text and quote it exactly",
            flag_id=str(flag.id),
            passages_checked=len(passage_ids),
        )
    review_item_id: str | None = None
    if args.verdict == "contradicted":
        item = ReviewRepository(db).create_item(
            kind="flag_audit",
            ref={"exposure_flag_id": str(flag.id)},
            reason=args.notes or f"auditor contradicted {flag.category} flag",
            priority=2,
        )
        review_item_id = str(item.id)
    verdict = {
        "flag_id": str(flag.id),
        "category": flag.category,
        "verdict": args.verdict,
        "passage_id": str(matched_passage),
        "review_item_id": review_item_id,
    }
    ctx.verdicts.append(verdict)
    return ToolResult(payload=dict(verdict, quote_found=True))


class ReportEstimateArgs(BaseModel):
    sample_size: int = Field(ge=1)
    verified: int = Field(ge=0)
    contradicted: int = Field(ge=0)
    estimated_error_rate: float = Field(
        ge=0.0, le=1.0, description="contradicted / sample, extrapolated to all flags"
    )
    notes: str | None = Field(default=None, max_length=1000)


def _report_estimate(db: Session, args: ReportEstimateArgs, ctx: ToolContext) -> ToolResult:
    if args.verified + args.contradicted > args.sample_size:
        return _error("verified + contradicted exceeds sample_size")
    estimate = args.model_dump(mode="json")
    recorded_verdicts = len(ctx.verdicts)
    contradicted_recorded = sum(1 for v in ctx.verdicts if v["verdict"] == "contradicted")
    estimate["verdicts_recorded"] = recorded_verdicts
    if args.verified + args.contradicted != recorded_verdicts:
        estimate["warning"] = (
            f"reported counts ({args.verified}+{args.contradicted}) differ from "
            f"verdicts actually recorded via verify_flag ({recorded_verdicts})"
        )
    if contradicted_recorded != args.contradicted:
        estimate.setdefault(
            "warning",
            f"contradicted count differs from recorded contradictions "
            f"({contradicted_recorded})",
        )
    ctx.estimate = estimate
    return ToolResult(payload=dict(estimate, recorded=True))


# ---------------------------------------------------------------------------
# Registration (the docs/plan.md §3 table, one row per tool)
# ---------------------------------------------------------------------------

_register(
    "get_run_stats",
    "Live counters, per-status document counts, and open-quarantine count for a processing run.",
    RunIdArgs,
    _get_run_stats,
)
_register(
    "get_class_failure_rates",
    "Per-file-class totals and quarantine/failure rates for a processing run.",
    RunIdArgs,
    _get_class_failure_rates,
)
_register(
    "list_quarantines",
    "List quarantine rows (default: open) with the condemned document's metadata.",
    ListQuarantinesArgs,
    _list_quarantines,
)
_register(
    "get_document_meta",
    "A document's recorded metadata: mimes, class, status, size, pages, quarantine history.",
    DocumentIdArgs,
    _get_document_meta,
)
_register(
    "get_raw_bytes_head",
    "Hex + printable preview of the first N bytes (max 512) of the stored original file.",
    RawBytesArgs,
    _get_raw_bytes_head,
)
_register(
    "sniff_type",
    "Re-sniff the stored bytes and report what the router would decide today.",
    DocumentIdArgs,
    _sniff_type,
)
_register(
    "try_parser",
    "Attempt a specific parser against the stored bytes; returns passage stats or the exact failure.",
    TryParserArgs,
    _try_parser,
)
_register(
    "run_ocr",
    "Rasterize one page (pdf/png) at a chosen DPI, optionally deskew+denoise, and OCR it.",
    RunOcrArgs,
    _run_ocr,
)
_register(
    "get_page_image",
    "Render one page (pdf/png) as an image so it can be read visually.",
    PageImageArgs,
    _get_page_image,
)
_register(
    "get_mention_evidence",
    "A mention with its passage preview and every attached PII element (normalized values).",
    MentionIdArgs,
    _get_mention_evidence,
)
_register(
    "get_person_summary",
    "A person's identity card: aliases, DOB, active mention/document counts, exposure flags.",
    PersonIdArgs,
    _get_person_summary,
)
_register(
    "compare_features",
    "Compute the deterministic ER feature vector and score for a mention pair.",
    ComparePairArgs,
    _compare_features,
)
_register(
    "search_elements",
    "Find PII elements by exact (element_type, normalized value) -- the ER blocking index.",
    SearchElementsArgs,
    _search_elements,
)
_register(
    "decide",
    "Record the adjudication for a mention pair (merge | no_merge | escalate). Merges on "
    "conflicting strong identifiers or name-only evidence are refused by the tool; merges "
    "touching a large cluster stop at a human approval gate.",
    DecideArgs,
    _decide,
    mutating=True,
)
_register(
    "verify_flag",
    "Record an audit verdict for an exposure flag. The quoted_span must appear VERBATIM in "
    "one of the flag's evidence passages or the verdict is refused.",
    VerifyFlagArgs,
    _verify_flag,
    mutating=True,
)
_register(
    "resolve_quarantine",
    "Mark an open quarantine agent_resolved with a corrected route and re-enqueue the document.",
    ResolveQuarantineArgs,
    _resolve_quarantine,
    mutating=True,
)
_register(
    "escalate",
    "Give up cleanly: record a structured diagnosis and escalate the current case to humans.",
    EscalateArgs,
    _escalate,
    mutating=True,
)
_register(
    "set_batch_priority",
    "Directive: reorder pipeline processing so one file class runs at the given priority.",
    SetBatchPriorityArgs,
    _set_batch_priority,
    mutating=True,
)
_register(
    "set_escalation_threshold",
    "Directive: move the tier-2 escalation confidence threshold (theta) for this run.",
    SetEscalationThresholdArgs,
    _set_escalation_threshold,
    mutating=True,
)
_register(
    "dispatch_investigator",
    "Directive: queue the exception investigator on one open quarantine.",
    DispatchInvestigatorArgs,
    _dispatch_investigator,
    mutating=True,
)
_register(
    "request_approval",
    "Create a human approval request (bulk_merge | final_signoff | class_pause) and pause "
    "until a person decides.",
    RequestApprovalArgs,
    _request_approval,
    mutating=True,
)
_register(
    "get_flag_with_evidence",
    "An exposure flag with its person and every passage-anchored evidence row.",
    FlagIdArgs,
    _get_flag_with_evidence,
)
_register(
    "get_passage_text",
    "The full text of one passage (the evidence anchor unit), with locator.",
    PassageTextArgs,
    _get_passage_text,
)
_register(
    "report_estimate",
    "Report the audit's measured sample counts and extrapolated error estimate.",
    ReportEstimateArgs,
    _report_estimate,
)


def registry() -> Mapping[str, ToolSpec]:
    return REGISTRY
