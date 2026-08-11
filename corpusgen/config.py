"""Corpus size and scenario quotas (docs/plan.md §8 / §14).

Two profiles: `full` (the scored 520-doc corpus) and `mini` (~80 docs for
fast pipeline round-trips). Class-mix arithmetic (full):

    target_docs 360 (digital: pdf_digital/docx/xlsx/csv/txt/html)
  + scanned_docs 104 (20% of 520 — real volume for the OCR/vision path)
  + eml_docs 36  + png_docs 8  + problem kinds 6 x problem_sets 2 = 12
  = 520 total

`target_docs` is the DIGITAL fill target (BackgroundFiller stops there);
the scanned/eml/png/problem scenarios emit after it, so `total_docs` is
the corpus size validate.py enforces. Mini keeps the same ~1/7 proportions
with every problem kind present at least once.
"""

from __future__ import annotations

from dataclasses import dataclass

# One of each: password_pdf, truncated_pdf, zero_byte, xlsx_as_pdf,
# docx_as_txt, png_as_xlsx (renderers/problem_files.py).
PROBLEM_KINDS_PER_SET = 6


@dataclass(frozen=True)
class CorpusConfig:
    profile: str
    n_identities: int
    # Digital-doc total the BackgroundFiller scenario fills up to; the
    # post-fill scenarios (scanned/eml/png/problem) add on top of it.
    target_docs: int
    # ScannedBatch: prose docs rendered via scanned_pdf (rasterize+degrade,
    # image-only pages) — plantings carry presentation "image".
    scanned_docs: int
    # EmailThreads: multipart .eml docs; a subset carry docx/xlsx/pdf
    # attachments, and eml_shared_attachment_pairs of them attach IDENTICAL
    # bytes (the sha256-dedup measurable).
    eml_docs: int
    eml_shared_attachment_pairs: int
    # PngScreenshots: table screenshots, including the BulkSpreadsheet
    # evil-twin PNG re-rendering the SAME sheet content as an image.
    png_docs: int
    # ProblemFiles: PROBLEM_KINDS_PER_SET kinds x this multiplier.
    problem_sets: int
    # NicknameCluster: persons with nickname-capable first names, each
    # appearing in nickname_docs_min..max docs under different name variants.
    nickname_cluster_persons: int
    nickname_docs_min: int
    nickname_docs_max: int
    # SharedName: pairs of DIFFERENT people sharing a full name (must never
    # be merged by ER — distinguished by DOB/SSN/address).
    shared_name_pairs: int
    # PartialIdentifiers: persons whose SSN appears name-free in a cred dump,
    # joined to a name-bearing doc only via employee_id (+ last-4 reference).
    partial_identifier_persons: int
    partial_dump_chunk: int  # persons per cred-dump document
    # BulkSpreadsheet: one xlsx exposing this many persons as rows.
    bulk_spreadsheet_rows: int
    # FalsePositiveTraps: dedicated trap documents (5 trap kinds cycled).
    trap_docs: int
    # Fraction of non-nickname-cluster identities that get a maiden-name
    # variant (nickname-cluster persons always get one, so the cluster can
    # exercise every variant kind).
    maiden_fraction: float

    @property
    def problem_docs(self) -> int:
        return PROBLEM_KINDS_PER_SET * self.problem_sets

    @property
    def total_docs(self) -> int:
        return (
            self.target_docs
            + self.scanned_docs
            + self.eml_docs
            + self.png_docs
            + self.problem_docs
        )


FULL = CorpusConfig(
    profile="full",
    n_identities=160,
    target_docs=360,
    scanned_docs=104,
    eml_docs=36,
    eml_shared_attachment_pairs=2,
    png_docs=8,
    problem_sets=2,
    nickname_cluster_persons=12,
    nickname_docs_min=3,
    nickname_docs_max=5,
    shared_name_pairs=5,
    partial_identifier_persons=10,
    partial_dump_chunk=4,
    bulk_spreadsheet_rows=80,
    trap_docs=15,
    maiden_fraction=0.3,
)

MINI = CorpusConfig(
    profile="mini",
    n_identities=40,
    target_docs=52,
    scanned_docs=15,
    eml_docs=6,
    eml_shared_attachment_pairs=1,
    png_docs=2,
    problem_sets=1,
    nickname_cluster_persons=4,
    nickname_docs_min=3,
    nickname_docs_max=5,
    shared_name_pairs=2,
    partial_identifier_persons=3,
    partial_dump_chunk=4,
    bulk_spreadsheet_rows=20,
    trap_docs=5,
    maiden_fraction=0.3,
)

PROFILES: dict[str, CorpusConfig] = {"full": FULL, "mini": MINI}

assert FULL.total_docs == 520, FULL.total_docs
