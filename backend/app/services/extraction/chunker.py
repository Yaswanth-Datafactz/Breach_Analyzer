"""Aggregate a document's passages into tier-1 chunks (docs/plan.md §9:
"other text -> tier 1 per ~2-3K-token passage-aligned chunk"; §14b binding:
"docx = 1 passage/paragraph ... tier-1 chunker must aggregate passages to
~2-3K tokens and never trust seq adjacency (python-docx yields paragraphs
before tables; use locators)").

Three chunk kinds come out of one pass over the passages:

- ``text``: consecutive passages of one LOCATOR FAMILY (docx paragraphs by
  paragraph number, docx tables by table number, pdf pages by page number,
  html/txt line ranges by line_start, email parts by seq) concatenated up
  to the token budget. Families are never mixed in one chunk -- that is the
  §14b rule made structural: a paragraph run and a table are adjacent by
  seq but not by document geometry, so seq adjacency across locator kinds
  is never trusted. Chunk text is a byte-verbatim concatenation of the
  passage texts joined by "\\n\\n", and every chunk carries a span map
  (chunk offset range -> passage), so an extracted value located in the
  chunk resolves back to its passage + passage-local offsets (D9).
- ``sheet_sample``: per sheet whose locator carries the deterministic
  header_map (parsing/xlsx.py + csv_.py emit it), ONE chunk containing
  only the UNMAPPED columns of a small sample of rows. Mapped columns are
  the deterministic sheet extractor's job (extraction/service.py), so the
  80-person sheet costs ~ one tier-1 call, not 80 (§9). A sheet with no
  unmapped columns produces no LLM chunk at all. Sample chunks are NOT
  verbatim concatenations -- their elements are located per passage.
- ``vision``: one chunk per image-based passage whose OCR mean confidence
  is below the vision threshold (§9: "low OCR confidence -> tier-2
  vision"; §14b R1: the BulkSpreadsheet PNGs recovered 1/327 values by
  OCR -- text-tier calls on that garbage would be wasted spend, so these
  passages are routed straight to the tier-2 vision path instead of into
  text chunks).

A single passage larger than the budget becomes its own oversized chunk --
passages are never split, because D9's offset computation is anchored to
the passage text and a split would manufacture a second coordinate system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.core.logging import get_logger
from app.db.models import Passage
from app.services.extraction.offsets import locate_values
from app.services.extraction.schemas import ElementOut

logger = get_logger("extraction.chunker")

_JOIN = "\n\n"
# Cell separator used by parsing/xlsx.py and docx table passages.
# parsing/csv_.py passages are the RAW source lines (comma-separated, by
# that module's substring-find rationale) -- sheet_separator() below picks
# the right separator per passage, keyed off the locator vocabulary the two
# parsers already distinguish themselves by (xlsx: row_start; csv:
# line_start). Without this, every csv data row split as ONE cell and the
# deterministic sheet pass minted whole-row mentions (name+SSN+DOB+... in
# name_raw) from every csv document.
CELL_SEPARATOR = " | "
_CSV_SEPARATOR = ","


def sheet_separator(passage: Passage) -> str:
    """The cell separator a sheet_range passage's text actually uses.
    Note csv cells are split on the bare comma: corpusgen's csv renderer
    never quotes (values contain no commas), and the extraction service
    verifies every computed cell slice against the passage text before
    persisting, so a quoted cell degrades to a skipped cell, never a wrong
    anchor."""
    locator = passage.locator or {}
    if "row_start" in locator:  # parsing/xlsx.py vocabulary
        return CELL_SEPARATOR
    if "line_start" in locator:  # parsing/csv_.py vocabulary
        return _CSV_SEPARATOR
    return CELL_SEPARATOR if CELL_SEPARATOR in passage.text.split("\n", 1)[0] else _CSV_SEPARATOR

TEXT = "text"
SHEET_SAMPLE = "sheet_sample"
VISION = "vision"


def estimate_tokens(text: str) -> int:
    """~4 chars/token -- routing needs an estimate, not a tokenizer."""
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class ChunkSpan:
    """One passage's byte range within a verbatim chunk's text.
    chunk_end is exclusive; for verbatim chunks
    chunk.text[chunk_start:chunk_end] == passage.text."""

    passage: Passage
    chunk_start: int
    chunk_end: int


@dataclass(frozen=True)
class Chunk:
    kind: str  # TEXT | SHEET_SAMPLE | VISION
    text: str
    spans: tuple[ChunkSpan, ...]
    # True when `text` is a verbatim concatenation of the span passages
    # (TEXT chunks); SHEET_SAMPLE and VISION chunks locate against the
    # passage text directly.
    verbatim: bool = True
    # SHEET_SAMPLE only: which sheet, which 1-based columns were unmapped
    # (the ones the model sees), and which spreadsheet row numbers were
    # sampled -- the service's projection pass consumes these.
    sheet: str | None = None
    unmapped_columns: tuple[int, ...] = ()
    sampled_row_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class ChunkLocatedElement:
    """An extracted element resolved back through the span map: the passage
    it lives in plus PASSAGE-LOCAL offsets (D9: offsets are into the passage
    text; passage.text[char_start:char_end] == element.value_raw whenever
    offsets are present)."""

    element: ElementOut
    passage: Passage | None
    char_start: int | None
    char_end: int | None

    @property
    def offsets_missing(self) -> bool:
        return self.char_start is None or self.passage is None


# ---------------------------------------------------------------------------
# Family assignment (the never-across-kinds rule)
# ---------------------------------------------------------------------------


def _family_and_order(passage: Passage) -> tuple[tuple, tuple]:
    """(family key, sort key within family). Passages only ever aggregate
    with same-family neighbours, ordered by their LOCATOR coordinates --
    never by bare seq across kinds (§14b)."""
    locator = passage.locator or {}
    if passage.kind == "text_block":
        if "paragraph" in locator:
            return ("docx_paragraphs",), (locator["paragraph"],)
        if "table" in locator:
            return ("docx_tables",), (locator["table"],)
        return ("text_blocks",), (passage.seq,)
    if passage.kind == "page":
        return ("pages",), (locator.get("page", passage.seq),)
    if passage.kind == "email_part":
        return ("email",), (passage.seq,)
    if passage.kind == "sheet_range":
        # Only reaches here for sheets WITHOUT a header_map (build_chunks
        # splits mapped sheets off first).
        return ("sheet_text", locator.get("sheet", "")), (
            locator.get("row_start", locator.get("line_start", passage.seq)),
        )
    return ("lines",), (locator.get("line_start", passage.seq),)


def _is_low_conf_image(passage: Passage, vision_conf_threshold: float) -> bool:
    if not passage.ocr or passage.page_image_sha is None:
        return False
    conf = (passage.locator or {}).get("ocr_mean_conf")
    return conf is not None and float(conf) < vision_conf_threshold


# ---------------------------------------------------------------------------
# Sheet sample chunks
# ---------------------------------------------------------------------------


def split_sheet_rows(passage: Passage) -> list[tuple[int, int, list[str]]]:
    """Decompose a sheet_range passage's text into data rows:
    (spreadsheet row number, char offset of the line start in passage.text,
    cells). Line 0 is the repeated header line (parsing/xlsx.py convention);
    data rows start at locator['row_start']."""
    locator = passage.locator or {}
    row_start = locator.get("row_start", locator.get("line_start", 1))
    separator = sheet_separator(passage)
    rows: list[tuple[int, int, list[str]]] = []
    offset = 0
    for index, line in enumerate(passage.text.split("\n")):
        if index > 0:  # skip the header line
            rows.append((row_start + index - 1, offset, line.split(separator)))
        offset += len(line) + 1
    return rows


def cell_offsets(
    line_offset: int, cells: list[str], col: int, *, separator: str = CELL_SEPARATOR
) -> tuple[int, int]:
    """Passage-text offsets of the 1-based `col`-th cell in a row line whose
    line starts at `line_offset` -- exact arithmetic over the known
    separator, no searching, so duplicate cell values cannot mislead.
    `separator` must be the one the passage text actually uses
    (sheet_separator(passage))."""
    start = line_offset
    for cell in cells[: col - 1]:
        start += len(cell) + len(separator)
    return start, start + len(cells[col - 1])


def _sheet_sample_chunk(
    sheet: str, passages: list[Passage], header_map: list[dict], sample_rows: int
) -> Chunk | None:
    unmapped = [
        entry
        for entry in header_map
        if entry.get("element_type") is None and (entry.get("header") or "").strip()
    ]
    if not unmapped:
        logger.info(
            "sheet_fully_mapped", sheet=sheet, columns=len(header_map), llm_calls=0
        )
        return None

    first = passages[0]
    rows = split_sheet_rows(first)[:sample_rows]
    lines = [
        f'Spreadsheet "{sheet}" -- a sample of rows is shown; only the columns '
        "whose meaning is not already known are included. Values are verbatim cell contents.",
    ]
    sampled_numbers: list[int] = []
    for row_number, _line_offset, cells in rows:
        parts = []
        for entry in unmapped:
            col = entry["col"]
            if col <= len(cells) and cells[col - 1]:
                parts.append(f"{entry['header']}: {cells[col - 1]}")
        if parts:
            sampled_numbers.append(row_number)
            lines.append(f"Row {row_number} | " + CELL_SEPARATOR.join(parts))
    if not sampled_numbers:
        return None  # unmapped columns exist but hold no data -- nothing to ask

    spans = tuple(ChunkSpan(passage=p, chunk_start=0, chunk_end=0) for p in passages)
    return Chunk(
        kind=SHEET_SAMPLE,
        text="\n".join(lines),
        spans=spans,
        verbatim=False,
        sheet=sheet,
        unmapped_columns=tuple(entry["col"] for entry in unmapped),
        sampled_row_numbers=tuple(sampled_numbers),
    )


# ---------------------------------------------------------------------------
# build_chunks
# ---------------------------------------------------------------------------


def build_chunks(
    passages: Sequence[Passage],
    *,
    max_tokens: int,
    sheet_sample_rows: int,
    vision_conf_threshold: float,
) -> list[Chunk]:
    """One pass over a document's passages -> the tier-1/tier-2 work list."""
    chunks: list[Chunk] = []

    # 1. Split off mapped sheets (deterministic-first, §9) and low-confidence
    #    image passages (vision route, §14b R1).
    sheet_groups: dict[str, list[Passage]] = {}
    text_passages: list[Passage] = []
    for passage in passages:
        locator = passage.locator or {}
        if passage.kind == "sheet_range" and locator.get("header_map"):
            sheet_groups.setdefault(locator.get("sheet", ""), []).append(passage)
        elif _is_low_conf_image(passage, vision_conf_threshold):
            chunks.append(
                Chunk(
                    kind=VISION,
                    text=passage.text,
                    spans=(
                        ChunkSpan(passage=passage, chunk_start=0, chunk_end=len(passage.text)),
                    ),
                    verbatim=False,
                )
            )
        elif passage.text.strip():
            text_passages.append(passage)

    for sheet, group in sheet_groups.items():
        group.sort(key=lambda p: (p.locator or {}).get("row_start", p.seq))
        header_map = (group[0].locator or {}).get("header_map") or []
        sample = _sheet_sample_chunk(sheet, group, header_map, sheet_sample_rows)
        if sample is not None:
            chunks.append(sample)

    # 2. Aggregate the rest per locator family, in locator order, greedily up
    #    to the token budget.
    families: dict[tuple, list[Passage]] = {}
    for passage in text_passages:
        family, _ = _family_and_order(passage)
        families.setdefault(family, []).append(passage)

    for family in sorted(families, key=str):
        members = sorted(families[family], key=lambda p: _family_and_order(p)[1])
        current: list[Passage] = []
        current_tokens = 0
        for passage in members:
            passage_tokens = estimate_tokens(passage.text)
            if current and current_tokens + passage_tokens > max_tokens:
                chunks.append(_verbatim_chunk(current))
                current, current_tokens = [], 0
            current.append(passage)
            current_tokens += passage_tokens
        if current:
            chunks.append(_verbatim_chunk(current))

    return chunks


def _verbatim_chunk(passages: list[Passage]) -> Chunk:
    spans: list[ChunkSpan] = []
    parts: list[str] = []
    offset = 0
    for passage in passages:
        spans.append(
            ChunkSpan(passage=passage, chunk_start=offset, chunk_end=offset + len(passage.text))
        )
        parts.append(passage.text)
        offset += len(passage.text) + len(_JOIN)
    return Chunk(kind=TEXT, text=_JOIN.join(parts), spans=tuple(spans), verbatim=True)


# ---------------------------------------------------------------------------
# Span-map resolution (chunk -> passage + passage-local offsets)
# ---------------------------------------------------------------------------


def locate_in_chunk(chunk: Chunk, elements: Sequence[ElementOut]) -> list[ChunkLocatedElement]:
    """Resolve every element's value_raw to (passage, passage-local offsets)
    via extraction/offsets.py's exact-match discipline (D9).

    Verbatim chunks: locate against the chunk text (occurrence order
    disambiguates duplicates exactly as the model saw them), then map the
    chunk offset through the span map; the mapped slice is verified against
    the PASSAGE text, so the invariant that ships downstream is always
    passage.text[char_start:char_end] == value_raw.

    Non-verbatim chunks (sheet samples, vision): locate against each span
    passage's text directly, in span order, with per-value cursors so
    repeated values consume successive occurrences.
    """
    if chunk.verbatim:
        located = locate_values(chunk.text, elements)
        results: list[ChunkLocatedElement] = []
        for item in located:
            if item.offsets_missing:
                results.append(
                    ChunkLocatedElement(element=item.element, passage=None, char_start=None, char_end=None)
                )
                continue
            span = _span_containing(chunk, item.char_start, item.char_end)
            if span is None:  # match straddles a passage boundary -- not a real anchor
                results.append(
                    ChunkLocatedElement(element=item.element, passage=None, char_start=None, char_end=None)
                )
                continue
            local_start = item.char_start - span.chunk_start
            local_end = item.char_end - span.chunk_start
            if span.passage.text[local_start:local_end] != item.element.value_raw:
                # Cannot happen for a verbatim span map; guarded anyway --
                # a wrong anchor is worse than a missing one (defensibility).
                results.append(
                    ChunkLocatedElement(element=item.element, passage=None, char_start=None, char_end=None)
                )
                continue
            results.append(
                ChunkLocatedElement(
                    element=item.element,
                    passage=span.passage,
                    char_start=local_start,
                    char_end=local_end,
                )
            )
        return results

    # Non-verbatim: search each span passage in order, cursor per value.
    cursors: dict[str, tuple[int, int]] = {}  # value -> (span index, offset)
    results = []
    for element in elements:
        value = element.value_raw
        span_index, offset = cursors.get(value, (0, 0))
        placed = False
        while span_index < len(chunk.spans):
            passage = chunk.spans[span_index].passage
            start = passage.text.find(value, offset)
            if start != -1:
                cursors[value] = (span_index, start + len(value))
                results.append(
                    ChunkLocatedElement(
                        element=element,
                        passage=passage,
                        char_start=start,
                        char_end=start + len(value),
                    )
                )
                placed = True
                break
            span_index, offset = span_index + 1, 0
        if not placed:
            cursors[value] = (len(chunk.spans), 0)
            logger.warning(
                "offsets_missing_in_chunk",
                chunk_kind=chunk.kind,
                element_type=element.element_type.value,
                value_length=len(value),
            )
            results.append(
                ChunkLocatedElement(element=element, passage=None, char_start=None, char_end=None)
            )
    return results


def _span_containing(chunk: Chunk, start: int, end: int) -> ChunkSpan | None:
    for span in chunk.spans:
        if span.chunk_start <= start and end <= span.chunk_end:
            return span
    return None
