"""PNG renderer: screenshot-style image of a spreadsheet (Pillow-drawn
shaded header row + gridded data rows) — the "image of a spreadsheet"
evil twin for BulkSpreadsheet and the small table screenshots. Table-only
specs, one table per image.

Text uses Pillow's bundled Aileron via ImageFont.load_default(size=...)
— deterministic and machine-independent (no system font lookup) — at a
size Tesseract reads near-perfectly, because unlike scanned PDFs these
are crisp screenshots: validate.py still matches OCR-tolerantly
(fuzz>=85) but recovery here should be ~100%.

Locations: {"row": n} — 1-based over DATA rows (the header row is row 0's
chrome, not a location). Presentation is forced to "image". PNG output
carries no timestamps — bytes are stable for a given spec.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from corpusgen.renderers import DocumentSpec, Table, planting_dict

_FONT_SIZE = 18
_PAD_X = 14
_PAD_Y = 8
_MARGIN = 24
_HEADER_BG = (222, 226, 232)
_GRID = (176, 180, 187)
_TEXT = (20, 22, 26)
_TITLE_GAP = 14


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    tables = [b for b in spec.blocks if isinstance(b, Table)]
    assert len(tables) == 1, "png specs are single-table"
    table = tables[0]

    font = ImageFont.load_default(size=_FONT_SIZE)
    title_font = ImageFont.load_default(size=_FONT_SIZE + 2)

    def text_w(text: str, f: ImageFont.FreeTypeFont) -> int:
        return int(f.getbbox(text)[2])

    col_widths = [
        max(text_w(str(cell), font) for cell in [header, *(row[c] for row in table.rows)])
        + 2 * _PAD_X
        for c, header in enumerate(table.headers)
    ]
    row_h = _FONT_SIZE + 2 * _PAD_Y
    title_h = _FONT_SIZE + 2 + _TITLE_GAP
    width = sum(col_widths) + 2 * _MARGIN
    height = title_h + row_h * (1 + len(table.rows)) + 2 * _MARGIN

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.text((_MARGIN, _MARGIN), spec.title, font=title_font, fill=_TEXT)

    top = _MARGIN + title_h
    left = _MARGIN
    # Header row.
    draw.rectangle(
        [left, top, left + sum(col_widths), top + row_h], fill=_HEADER_BG
    )
    x = left
    for c, header in enumerate(table.headers):
        draw.text((x + _PAD_X, top + _PAD_Y), header, font=font, fill=_TEXT)
        x += col_widths[c]
    # Data rows.
    for r, row in enumerate(table.rows):
        y = top + row_h * (1 + r)
        x = left
        for c, cell in enumerate(row):
            draw.text((x + _PAD_X, y + _PAD_Y), str(cell), font=font, fill=_TEXT)
            x += col_widths[c]
    # Grid.
    bottom = top + row_h * (1 + len(table.rows))
    for r in range(len(table.rows) + 2):
        y = top + row_h * r
        draw.line([left, y, left + sum(col_widths), y], fill=_GRID)
    x = left
    for w in [0, *col_widths]:
        x += w
        draw.line([x, top, x, bottom], fill=_GRID)

    img.save(path, "PNG")

    plantings: list[dict] = []
    for row_idx, col_idx, plant in table.cell_plants:
        assert plant.value in table.rows[row_idx][col_idx]
        plant.presentation = "image"
        plantings.append(planting_dict(plant, {"row": row_idx + 1}))
    return plantings
