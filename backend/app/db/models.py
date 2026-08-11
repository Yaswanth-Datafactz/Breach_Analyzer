"""SQLAlchemy 2.x ORM models for the full docs/plan.md §4 schema.

Style mirrors Document_Extraction/backend/app/db/models.py (UC2): UUID
primary keys are app-generated via `uuid.uuid4` (not a DB extension) since
document/person/run ids are exposed through the REST API and sequential
integer ids would let a caller enumerate records; created_at/updated_at are
server defaults; enums are String columns constrained by named CHECKs (the
plan's enum lists are closed sets, and a CHECK makes "nothing silently
dropped" a database guarantee, not a convention).

Three schema decisions worth calling out:

- `extraction_jobs` carries UC2's partial-unique trick: at most one *live*
  job per document (`uq_extraction_jobs_active_document`), which is what the
  startup stale-job reaper keys off.
- `identity_links` is append-only (unlink = new inactive row -- every merge/
  split replayable, docs/plan.md D5), so the plan's "mention_id UNIQUE" is
  implemented as a partial unique index over active rows only
  (`uq_identity_links_active_mention`): one *live* link per mention, full
  history preserved. A plain UNIQUE would make append-only impossible.
- `pii_elements` gets the (element_type, value_normalized) composite index --
  the ER blocking index (docs/plan.md D5): exact-identifier candidate lookup
  must not scan.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Statuses that mean "this job will never change again" -- used both by the
# partial unique index below (at most one *live* job per document, UC2's
# `uq_extraction_jobs_active_document` trick) and by the stale-job reaper
# (services/pipeline.py) to decide what counts as orphaned on startup.
EXTRACTION_JOB_TERMINAL_STATUSES = ("done", "failed", "quarantined")

# The exposure categories (docs/plan.md §1) -- exposure_flags.category and
# the accuracy flag results share this closed set.
EXPOSURE_CATEGORIES = (
    "ssn",
    "dob",
    "drivers_license",
    "passport",
    "financial_account",
    "credit_card",
    "medical",
    "credentials",
    "address",
    "phone",
    "email",
)

_EXPOSURE_CATEGORY_SQL = ", ".join(f"'{c}'" for c in EXPOSURE_CATEGORIES)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'running', 'paused', 'finished', 'failed')",
            name="valid_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Models, thresholds, git_sha, corpus seed -- the replayability anchor
    # (docs/plan.md §3: the pipeline is replayable from documents + config
    # snapshot).
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="created")
    counters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    documents: Mapped[list["Document"]] = relationship(back_populates="run", passive_deletes=True)
    persons: Mapped[list["Person"]] = relationship(back_populates="run", passive_deletes=True)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_documents_sha256"),
        Index("ix_documents_run_id_status", "run_id", "status"),
        CheckConstraint(
            "file_class IN ('pdf_digital', 'pdf_scanned', 'docx', 'xlsx', 'csv', 'eml', 'msg', "
            "'html', 'txt', 'png', 'unknown')",
            name="valid_file_class",
        ),
        CheckConstraint("source_kind IN ('corpus', 'attachment')", name="valid_source_kind"),
        CheckConstraint(
            "status IN ('queued', 'parsed', 'extracted', 'quarantined', 'failed', 'done')",
            name="valid_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    rel_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Both kept deliberately (docs/plan.md §3: MIME-sniff vs extension at
    # ingest) -- a mismatch is itself a routing/quarantine signal.
    declared_mime: Mapped[str | None] = mapped_column(String(100))
    sniffed_mime: Mapped[str | None] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_class: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="corpus")
    # Email attachments are recursively re-ingested as child documents
    # (docs/plan.md §3) -- SET NULL, not CASCADE: an attachment row must
    # survive its parent being purged, it has its own evidence links.
    parent_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    page_count: Mapped[int | None] = mapped_column(Integer)
    # Set once the routing heuristic has actually inspected the pages --
    # null between ingest and that point, never guessed from the filename.
    is_image_based: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    run: Mapped["ProcessingRun"] = relationship(back_populates="documents")
    passages: Mapped[list["Passage"]] = relationship(back_populates="document", passive_deletes=True)
    quarantines: Mapped[list["Quarantine"]] = relationship(
        back_populates="document", passive_deletes=True
    )
    jobs: Mapped[list["ExtractionJob"]] = relationship(
        back_populates="document", passive_deletes=True
    )


class Passage(Base):
    """The evidence anchor unit (docs/plan.md §4): every pii_element and
    every flag_evidence row resolves to one of these, with char offsets."""

    __tablename__ = "passages"
    __table_args__ = (
        Index("ix_passages_document_id_seq", "document_id", "seq"),
        CheckConstraint(
            "kind IN ('page', 'sheet_range', 'email_part', 'text_block')", name="valid_kind"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # {page: 3} | {sheet: "Employees", rows: "2-40"} | {part: "body_text"} --
    # the UI's locator breadcrumb renders straight from this.
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    page_image_sha: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="passages")


class Quarantine(Base):
    """Invariant (docs/plan.md §4): every document ends `done` or has one of
    these rows -- the reconciliation query in §15 must return zero."""

    __tablename__ = "quarantines"
    __table_args__ = (
        Index("ix_quarantines_status", "status"),
        CheckConstraint("stage IN ('ingest', 'parse', 'extract')", name="valid_stage"),
        CheckConstraint(
            "reason_code IN ('password_protected', 'corrupt', 'zero_byte', 'wrong_extension', "
            "'unsupported', 'ocr_garbage', 'parser_error')",
            name="valid_reason_code",
        ),
        CheckConstraint(
            "status IN ('open', 'agent_resolved', 'escalated', 'human_resolved')",
            name="valid_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    resolved_by_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="quarantines")


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    __table_args__ = (
        Index("ix_extraction_jobs_status_created_at", "status", "created_at"),
        # UC2's partial-unique trick, kept byte-for-byte in spirit: at most
        # one non-terminal job per document.
        Index(
            "uq_extraction_jobs_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("status NOT IN ('done', 'failed', 'quarantined')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    # Which tiers this document actually traversed and why, e.g.
    # [{"tier": 1, "model": "DeepSeek-V3.2"}, {"tier": 2, "reason": "low_confidence"}]
    tier_path: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="jobs")


class Mention(Base):
    """The unit ER clusters (docs/plan.md §4): one person-reference in one
    passage, with the features scoring uses."""

    __tablename__ = "mentions"
    __table_args__ = (
        Index("ix_mentions_document_id", "document_id"),
        Index("ix_mentions_name_phonetic", "name_phonetic"),
        CheckConstraint(
            "detector IN ('llm_tier1', 'llm_tier2', 'deterministic_sheet')", name="valid_detector"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    passage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("passages.id", ondelete="CASCADE"), nullable=False
    )
    name_raw: Mapped[str] = mapped_column(String(300), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    # Double metaphone key -- the phonetic blocking key (docs/plan.md D5).
    name_phonetic: Mapped[str | None] = mapped_column(String(50))
    dob: Mapped[date | None] = mapped_column(Date)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    detector: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PiiElement(Base):
    __tablename__ = "pii_elements"
    __table_args__ = (
        # The ER blocking index (docs/plan.md §4): exact SSN/email/phone/
        # account candidate lookup.
        Index("ix_pii_elements_element_type_value_normalized", "element_type", "value_normalized"),
        Index("ix_pii_elements_document_id", "document_id"),
        CheckConstraint(
            "element_type IN ('ssn', 'ssn_last4', 'dob', 'drivers_license', 'passport', "
            "'financial_account', 'credit_card', 'medical', 'credential', 'address', 'phone', "
            "'email', 'name')",
            name="valid_element_type",
        ),
        CheckConstraint(
            "detector IN ('regex', 'checksum', 'llm_tier1', 'llm_tier2', 'reviewer')",
            name="valid_detector",
        ),
        CheckConstraint(
            "validation_status IN ('valid', 'invalid_checksum', 'format_only')",
            name="valid_validation_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    passage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("passages.id", ondelete="CASCADE"), nullable=False
    )
    # NULLABLE deliberately (docs/plan.md §4): a partial identifier (SSN
    # alone in doc A) has no person mention yet -- ER attaches it later.
    mention_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mentions.id", ondelete="SET NULL")
    )
    element_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # NEVER logged (core/logging.py's redaction processor; CLAUDE.md hard rule).
    value_raw: Mapped[str] = mapped_column(Text, nullable=False)
    value_normalized: Mapped[str] = mapped_column(String(500), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    context_snippet: Mapped[str | None] = mapped_column(Text)
    detector: Mapped[str] = mapped_column(String(20), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="format_only")
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    signals: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Person(Base):
    """One row per resolved unique individual -- the exposure table's spine."""

    __tablename__ = "persons"
    __table_args__ = (
        Index("ix_persons_run_id", "run_id"),
        CheckConstraint(
            "review_status IN ('auto', 'needs_review', 'human_confirmed')", name="valid_review_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    best_name: Mapped[str] = mapped_column(String(300), nullable=False)
    # [{name, kind: nickname|maiden|initials|misspelling|order_variant}]
    aliases: Mapped[list | None] = mapped_column(JSONB)
    dob: Mapped[date | None] = mapped_column(Date)
    er_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    run: Mapped["ProcessingRun"] = relationship(back_populates="persons")
    identity_links: Mapped[list["IdentityLink"]] = relationship(
        back_populates="person", passive_deletes=True
    )
    exposure_flags: Mapped[list["ExposureFlag"]] = relationship(
        back_populates="person", passive_deletes=True
    )


class IdentityLink(Base):
    """Append-only (docs/plan.md D5): unlink = new inactive row, so every
    merge/split is replayable. The plan's "mention_id UNIQUE" is therefore a
    PARTIAL unique index over active rows (one *live* link per mention) --
    UC2's active-row trick again; a plain UNIQUE would forbid history."""

    __tablename__ = "identity_links"
    __table_args__ = (
        Index(
            "uq_identity_links_active_mention",
            "mention_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index("ix_identity_links_person_id", "person_id"),
        CheckConstraint("method IN ('rule', 'agent', 'reviewer')", name="valid_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    mention_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mentions.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    # e.g. 'exact_ssn', 'name_dob_phone' -- every link carries rule-or-agent
    # rationale (docs/plan.md D5).
    rule_id: Mapped[str | None] = mapped_column(String(50))
    rationale: Mapped[str | None] = mapped_column(Text)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    person: Mapped["Person"] = relationship(back_populates="identity_links")


class ErDecision(Base):
    __tablename__ = "er_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('merge', 'no_merge', 'split', 'escalate')", name="valid_decision"
        ),
        CheckConstraint("method IN ('rule', 'agent', 'reviewer')", name="valid_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    left_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    right_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(5, 4))
    # Per-feature contributions -- the adjudicator's feature citations and
    # the review UI's side-by-side evidence both render from this.
    features: Mapped[dict | None] = mapped_column(JSONB)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approval_requests.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExposureFlag(Base):
    __tablename__ = "exposure_flags"
    __table_args__ = (
        UniqueConstraint("person_id", "category", name="uq_exposure_flags_person_id"),
        CheckConstraint(f"category IN ({_EXPOSURE_CATEGORY_SQL})", name="valid_category"),
        CheckConstraint(
            "review_status IN ('auto', 'human_confirmed', 'human_overridden')",
            name="valid_review_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    exposed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    review_status: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    person: Mapped["Person"] = relationship(back_populates="exposure_flags")
    evidence: Mapped[list["FlagEvidence"]] = relationship(
        back_populates="exposure_flag", passive_deletes=True
    )


class FlagEvidence(Base):
    """Invariant (docs/plan.md §4, enforced in services/exposure.py): no
    exposure flag without at least one of these rows -- defensibility is the
    bar, a flag that cannot show its source passage is worthless."""

    __tablename__ = "flag_evidence"
    __table_args__ = (Index("ix_flag_evidence_exposure_flag_id", "exposure_flag_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exposure_flag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exposure_flags.id", ondelete="CASCADE"), nullable=False
    )
    pii_element_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pii_elements.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    passage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("passages.id", ondelete="CASCADE"), nullable=False
    )
    snippet: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    exposure_flag: Mapped["ExposureFlag"] = relationship(back_populates="evidence")


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        Index("ix_review_items_kind_status", "kind", "status"),
        CheckConstraint("kind IN ('extraction', 'er_pair', 'flag_audit')", name="valid_kind"),
        CheckConstraint("status IN ('open', 'decided', 'dismissed')", name="valid_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # {pii_element_id} | {left_person_id, right_person_id} | {exposure_flag_id}
    # -- kind-shaped, hence JSONB rather than three nullable FKs.
    ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    decisions: Mapped[list["ReviewDecision"]] = relationship(
        back_populates="review_item", passive_deletes=True
    )


class ReviewDecision(Base):
    """Decisions project back (docs/plan.md §4): element correction ->
    recompute flags; ER decision -> identity_links. before/after JSONB keeps
    the correction auditable independent of what it projected onto."""

    __tablename__ = "review_decisions"
    __table_args__ = (Index("ix_review_decisions_review_item_id", "review_item_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    review_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("review_items.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reviewer: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    review_item: Mapped["ReviewItem"] = relationship(back_populates="decisions")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_agent_kind_status", "agent_kind", "status"),
        CheckConstraint(
            "agent_kind IN ('orchestrator', 'investigator', 'adjudicator', 'auditor')",
            name="valid_agent_kind",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'escalated', 'budget_exceeded', "
            "'awaiting_approval', 'failed')",
            name="valid_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    budget_max_steps: Mapped[int | None] = mapped_column(Integer)
    budget_max_tokens: Mapped[int | None] = mapped_column(Integer)
    budget_max_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    outcome: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="agent_run", passive_deletes=True, order_by="AgentStep.step_no"
    )


class AgentStep(Base):
    """Persisted BEFORE execution continues (docs/plan.md §3's AgentRunner
    mechanics) -- a crash still leaves an inspectable trace."""

    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "step_no", name="uq_agent_steps_agent_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    request_summary: Mapped[dict | None] = mapped_column(JSONB)
    response_summary: Mapped[dict | None] = mapped_column(JSONB)
    stop_reason: Mapped[str | None] = mapped_column(String(30))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    agent_run: Mapped["AgentRun"] = relationship(back_populates="steps")
    tool_calls: Mapped[list["AgentToolCall"]] = relationship(
        back_populates="agent_step", passive_deletes=True
    )


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"
    __table_args__ = (Index("ix_agent_tool_calls_agent_step_id", "agent_step_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[dict | None] = mapped_column(JSONB)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    agent_step: Mapped["AgentStep"] = relationship(back_populates="tool_calls")


class ApprovalRequest(Base):
    """The human gates (docs/plan.md §3): bulk merges, final sign-off, class
    pauses park the agent run until a human decides."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_status", "status"),
        CheckConstraint(
            "action_type IN ('bulk_merge', 'final_signoff', 'class_pause')", name="valid_action_type"
        ),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="valid_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CostEvent(Base):
    """Every LLM call writes one of these (docs/plan.md §9) -- the
    extrapolation tables and the cost dashboard are SQL over this table,
    never re-derived from provider dashboards."""

    __tablename__ = "cost_events"
    __table_args__ = (
        Index("ix_cost_events_run_id_purpose", "run_id", "purpose"),
        CheckConstraint(
            "purpose IN ('tier1', 'tier2_text', 'tier2_vision', 'agent_orchestrator', "
            "'agent_investigator', 'agent_adjudicator', 'agent_auditor', 'eval')",
            name="valid_purpose",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Nullable: eval-purpose calls happen outside a processing run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("processing_runs.id", ondelete="SET NULL")
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AccuracyRun(Base):
    """One row per scripts/run_accuracy_eval.py invocation (docs/plan.md §10)
    -- git-SHA-stamped config snapshot so any headline number is reproducible
    and attributable, UC2's convention."""

    __tablename__ = "accuracy_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Person P/R/F1, pairwise ER precision/recall, per-category P/R, trap
    # scorecard -- the rollup the design doc's tables render from.
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    person_results: Mapped[list["AccuracyPersonResult"]] = relationship(
        back_populates="accuracy_run", passive_deletes=True
    )
    flag_results: Mapped[list["AccuracyFlagResult"]] = relationship(
        back_populates="accuracy_run", passive_deletes=True
    )


class AccuracyPersonResult(Base):
    __tablename__ = "accuracy_person_results"
    __table_args__ = (
        Index("ix_accuracy_person_results_accuracy_run_id", "accuracy_run_id"),
        CheckConstraint(
            "outcome IN ('matched', 'missed', 'split', 'wrongly_merged', 'hallucinated')",
            name="valid_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    accuracy_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accuracy_runs.id", ondelete="CASCADE"), nullable=False
    )
    manifest_person_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    matched_person_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 'exact_ssn' | 'exact_email' | 'name_dob_fuzzy' | ... -- which of §10's
    # two matching stages (and which identifier) produced the match.
    match_basis: Mapped[str | None] = mapped_column(String(50))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)

    accuracy_run: Mapped["AccuracyRun"] = relationship(back_populates="person_results")


class AccuracyFlagResult(Base):
    __tablename__ = "accuracy_flag_results"
    __table_args__ = (
        Index("ix_accuracy_flag_results_accuracy_run_id", "accuracy_run_id"),
        CheckConstraint(f"category IN ({_EXPOSURE_CATEGORY_SQL})", name="valid_category"),
        CheckConstraint("outcome IN ('tp', 'fp', 'fn', 'tn')", name="valid_outcome"),
        CheckConstraint(
            "error_class IS NULL OR error_class IN ('missed_extraction', 'ocr_failure', "
            "'er_split', 'er_overmerge', 'trap_fp', 'wrong_category')",
            name="valid_error_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    accuracy_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accuracy_runs.id", ondelete="CASCADE"), nullable=False
    )
    manifest_person_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    expected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    predicted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(5), nullable=False)
    # Every non-TP row gets one (docs/plan.md §10) -- the error-analysis
    # section's drill-through key.
    error_class: Mapped[str | None] = mapped_column(String(30))

    accuracy_run: Mapped["AccuracyRun"] = relationship(back_populates="flag_results")


class ManifestIdentity(Base):
    """Ground-truth identities imported from data/manifest.json (docs/plan.md
    §10: imported to tables for SQL joins, never re-parsed per query)."""

    __tablename__ = "manifest_identities"
    __table_args__ = (UniqueConstraint("person_uid", name="uq_manifest_identities_person_uid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    full_name: Mapped[str] = mapped_column(String(300), nullable=False)
    dob: Mapped[date | None] = mapped_column(Date)
    # Name variants, strong identifiers, expected category set -- the
    # scenario objects emit this alongside the documents (docs/plan.md D6).
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ManifestElement(Base):
    """Ground-truth plantings: one row per (identity, document, element) the
    corpus generator recorded -- §15's gate query resolves ten of these to
    pii_elements at correct offsets."""

    __tablename__ = "manifest_elements"
    __table_args__ = (
        Index("ix_manifest_elements_person_uid", "person_uid"),
        Index("ix_manifest_elements_rel_path", "rel_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    rel_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    element_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the generator planted it: page/sheet/part plus char offsets when known.
    expected_locator: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
