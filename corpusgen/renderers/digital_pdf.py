"""Digital-PDF renderer (reportlab canvas, invariant mode).

Line-oriented writer so every plant's PAGE is known at draw time.
`invariant=1` pins the creation date and document ID — the same spec
produces byte-identical PDF output on every run. Logical lines are
word-wrapped but kept together across page breaks, so a planted value
never straddles two pages (validation matches whitespace-collapsed value
against the recorded page's text).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as rl_canvas

from corpusgen.renderers import DocumentSpec, Paragraph, Table, planting_dict

_MARGIN = 72
_LEADING = 14
_BODY_FONT = ("Helvetica", 10.5)
_TITLE_FONT = ("Helvetica-Bold", 13)
_WRAP_COLS = 92
_PAGE_W, _PAGE_H = letter


class _LineWriter:
    def __init__(self, canv: rl_canvas.Canvas) -> None:
        self.canv = canv
        self.page = 1
        self.y = _PAGE_H - _MARGIN

    def _new_page(self) -> None:
        self.canv.showPage()
        self.page += 1
        self.y = _PAGE_H - _MARGIN
        self.canv.setFont(*_BODY_FONT)

    def ensure_room(self, n_lines: int) -> None:
        if self.y - (n_lines - 1) * _LEADING < _MARGIN:
            self._new_page()

    def write(self, text: str, font: tuple[str, float] = _BODY_FONT) -> None:
        if self.y < _MARGIN:
            self._new_page()
        self.canv.setFont(*font)
        self.canv.drawString(_MARGIN, self.y, text)
        self.y -= _LEADING

    def gap(self, factor: float = 0.5) -> None:
        self.y -= _LEADING * factor


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    canv = rl_canvas.Canvas(str(path), pagesize=letter, invariant=1, pageCompression=0)
    canv.setTitle(spec.title)
    canv.setAuthor("Meridian Benefits Group")
    writer = _LineWriter(canv)
    plantings: list[dict] = []

    writer.write(spec.title, font=_TITLE_FONT)
    writer.write(spec.doc_date.isoformat(), font=("Helvetica", 9))
    writer.gap()

    for block in spec.blocks:
        if isinstance(block, Paragraph):
            # Track which page each logical line lands on, then locate each
            # plant by the line containing its value.
            line_pages: list[int] = []
            logical_lines = block.text.split("\n")
            for logical in logical_lines:
                wrapped = textwrap.wrap(logical, _WRAP_COLS) or [""]
                writer.ensure_room(len(wrapped))
                line_pages.append(writer.page)
                for physical in wrapped:
                    writer.write(physical)
            for plant in block.plants:
                page = next(
                    (
                        line_pages[i]
                        for i, logical in enumerate(logical_lines)
                        if plant.value in logical
                    ),
                    None,
                )
                assert page is not None, f"plant {plant.value!r} not in paragraph text"
                plantings.append(planting_dict(plant, {"page": page}))
        else:
            assert isinstance(block, Table)
            writer.ensure_room(2)
            writer.write(" | ".join(block.headers), font=("Helvetica-Bold", 10.5))
            row_pages: list[int] = []
            for row in block.rows:
                writer.ensure_room(1)
                row_pages.append(writer.page)
                writer.write(" | ".join(row))
            for row_idx, col_idx, plant in block.cell_plants:
                assert plant.value in block.rows[row_idx][col_idx]
                plantings.append(planting_dict(plant, {"page": row_pages[row_idx]}))
        writer.gap()

    canv.save()
    return plantings
