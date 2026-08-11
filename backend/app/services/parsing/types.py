"""Shared shapes for the per-type parsers (docs/plan.md §3's PARSE step).

Every parser returns a `ParseResult`: an ordered list of `ParsedPassage`
(the evidence anchor unit -- `passages` rows are persisted 1:1 from these,
seq assigned in list order) plus document-level facts only the parser can
know (page_count, is_image_based) and any embedded files it extracted
(email attachments -- recursively re-ingested as child documents by the
pipeline, never parsed inline, so an attachment gets the same dedup/
classify/quarantine treatment as a corpus file).

Locator vocabulary matches corpusgen's renderers so a manifest planting and
the passage holding it speak the same coordinates: {"page": n} for PDFs/
images, {"paragraph": n} / {"table": t, ...} for docx, {"sheet", "row_start",
"row_end"} for xlsx, {"line_start", "line_end"} for line-based text, and
{"part": "body"|"headers"} for email parts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedPassage:
    kind: str  # 'page' | 'sheet_range' | 'email_part' | 'text_block' (passages.kind CHECK)
    locator: dict
    text: str
    ocr: bool = False
    page_image_sha: str | None = None


@dataclass(frozen=True)
class ExtractedAttachment:
    filename: str
    content: bytes


@dataclass
class ParseResult:
    passages: list[ParsedPassage]
    page_count: int | None = None
    is_image_based: bool | None = None
    attachments: list[ExtractedAttachment] = field(default_factory=list)
