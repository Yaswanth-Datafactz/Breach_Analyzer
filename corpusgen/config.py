"""Corpus size and scenario quotas (docs/plan.md §8).

Two profiles: `full` (the scored 520-doc corpus) and `mini` (~60 docs for
fast pipeline round-trips). Today's generator covers the DIGITAL renderers
only (pdf_digital, docx, xlsx, csv, txt, html); the scanned/eml/png/
problem-file classes land next and will raise `target_docs` to the brief's
520 — `target_docs` here is the digital-only portion, and the extension
point is: add per-class quota fields + scenarios, bump `target_docs`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorpusConfig:
    profile: str
    n_identities: int
    # Digital-doc total the BackgroundFiller scenario fills up to (see module
    # docstring for why this is 420, not 520, today).
    target_docs: int
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


FULL = CorpusConfig(
    profile="full",
    n_identities=160,
    target_docs=420,
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
    target_docs=60,
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
