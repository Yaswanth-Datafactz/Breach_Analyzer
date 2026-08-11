"""CSV parsing (stdlib csv; module named csv_ to keep the stdlib import
unambiguous, mirroring the plan §6 naming convention for shadow-prone names).

Passage text is the RAW source lines, not a re-serialization: manifest
plantings and D9's computed offsets both resolve by exact substring find,
and round-tripping through a csv writer could alter quoting. The csv module
is used only to read the header row for the same deterministic header-
lexicon hook xlsx uses (services/parsing/xlsx.py) -- the per-file header
map rides in every passage locator for phase B2's deterministic sheet
extractor.

Locators are {"line_start", "line_end"} in 1-based source line numbers --
the same `line` vocabulary corpusgen's csv renderer records (line 1 is the
header row, so the first chunk starts at line_start=2). The header line is
repeated at the top of every chunk's text for column context, exactly like
the xlsx chunks.
"""

from __future__ import annotations

import csv
import io

from app.services.parsing.types import ParsedPassage, ParseResult
from app.services.parsing.xlsx import map_header

LINES_PER_PASSAGE = 50


def _build_header_map(header_line: str) -> list[dict]:
    try:
        headers = next(csv.reader(io.StringIO(header_line)))
    except (csv.Error, StopIteration):
        headers = []
    return [
        {"col": idx, "header": header, "element_type": map_header(header) if header else None}
        for idx, header in enumerate(headers, start=1)
    ]


def parse_csv(content: bytes) -> ParseResult:
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return ParseResult(
            passages=[
                ParsedPassage(
                    kind="sheet_range", locator={"line_start": 1, "line_end": 1}, text=""
                )
            ]
        )

    header_line = lines[0]
    header_map = _build_header_map(header_line)
    data_lines = lines[1:]

    if not data_lines:
        return ParseResult(
            passages=[
                ParsedPassage(
                    kind="sheet_range",
                    locator={"line_start": 1, "line_end": 1, "header_map": header_map},
                    text=header_line,
                )
            ]
        )

    passages: list[ParsedPassage] = []
    for chunk_start in range(0, len(data_lines), LINES_PER_PASSAGE):
        chunk = data_lines[chunk_start : chunk_start + LINES_PER_PASSAGE]
        line_start = chunk_start + 2  # data begins at source line 2
        line_end = line_start + len(chunk) - 1
        passages.append(
            ParsedPassage(
                kind="sheet_range",
                locator={
                    "line_start": line_start,
                    "line_end": line_end,
                    "header_map": header_map,
                },
                text="\n".join([header_line] + chunk),
            )
        )
    return ParseResult(passages=passages)
