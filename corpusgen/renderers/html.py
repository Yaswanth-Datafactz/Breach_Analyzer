"""HTML renderer (support-ticket-export style). Each paragraph and each
table row is emitted on its own source line so {"line": n} locations are
exact against the raw file. Values are asserted escape-stable (no &<>" in
any planted value), so raw-line containment holds after html.escape.
"""

from __future__ import annotations

import html
from pathlib import Path

from corpusgen.renderers import DocumentSpec, Paragraph, Table, planting_dict


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    lines: list[str] = [
        "<!doctype html>",
        f"<html><head><meta charset=\"utf-8\"><title>{html.escape(spec.title)}</title></head>",
        "<body>",
        f"<h1>{html.escape(spec.title)}</h1>",
        f"<p class=\"meta\">Exported {spec.doc_date.isoformat()}</p>",
    ]
    plantings: list[dict] = []

    for block in spec.blocks:
        if isinstance(block, Paragraph):
            block_lines = block.text.split("\n")
            line_nos: list[int] = []
            for text in block_lines:
                lines.append(f"<p>{html.escape(text)}</p>")
                line_nos.append(len(lines))  # 1-based
            for plant in block.plants:
                assert html.escape(plant.value) == plant.value
                line_no = next(
                    (line_nos[i] for i, text in enumerate(block_lines) if plant.value in text),
                    None,
                )
                assert line_no is not None, f"plant {plant.value!r} not in paragraph text"
                plantings.append(planting_dict(plant, {"line": line_no}))
        else:
            assert isinstance(block, Table)
            lines.append("<table>")
            lines.append("<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in block.headers) + "</tr>")
            row_line_nos: list[int] = []
            for row in block.rows:
                lines.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in row) + "</tr>")
                row_line_nos.append(len(lines))
            lines.append("</table>")
            for row_idx, col_idx, plant in block.cell_plants:
                assert html.escape(plant.value) == plant.value
                assert plant.value in block.rows[row_idx][col_idx]
                plantings.append(planting_dict(plant, {"line": row_line_nos[row_idx]}))

    lines.extend(["</body></html>"])
    path.write_text("\n".join(lines) + "\n")
    return plantings
