"""HTML and TXT parsing.

HTML: a stdlib html.parser tag-stripper (html2text-style, no dependency) --
script/style content is dropped, block-level closes emit newlines so the
visual line structure survives, entities are unescaped by the parser's
convert_charrefs default. Planted values therefore stay byte-exact in the
extracted text (corpusgen plants into element text, never into markup).
Note: manifest locations for html record SOURCE line numbers; passage
locators here are {"line_start","line_end"} over the EXTRACTED text --
accuracy matching (plan §10) resolves plantings by value find, not by
locator equality, so the two coordinate systems never need reconciling.

TXT: the decoded content chunked by line ranges, same locator shape.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from app.services.parsing.types import ParsedPassage, ParseResult

LINES_PER_PASSAGE = 200

_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "table", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
        "footer", "blockquote", "pre", "dt", "dd",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse intra-line whitespace runs and blank-line stacks --
        # markup indentation is noise, but single newlines are structure.
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        collapsed: list[str] = []
        for line in lines:
            if line or (collapsed and collapsed[-1]):
                collapsed.append(line)
        while collapsed and not collapsed[-1]:
            collapsed.pop()
        return "\n".join(collapsed)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def _chunk_lines(text: str, kind: str) -> list[ParsedPassage]:
    lines = text.splitlines() or [""]
    passages: list[ParsedPassage] = []
    for chunk_start in range(0, len(lines), LINES_PER_PASSAGE):
        chunk = lines[chunk_start : chunk_start + LINES_PER_PASSAGE]
        passages.append(
            ParsedPassage(
                kind=kind,
                locator={"line_start": chunk_start + 1, "line_end": chunk_start + len(chunk)},
                text="\n".join(chunk),
            )
        )
    return passages


def parse_html(content: bytes) -> ParseResult:
    text = html_to_text(content.decode("utf-8", errors="replace"))
    return ParseResult(passages=_chunk_lines(text, "text_block"))


def parse_txt(content: bytes) -> ParseResult:
    text = content.decode("utf-8", errors="replace")
    return ParseResult(passages=_chunk_lines(text, "text_block"))
