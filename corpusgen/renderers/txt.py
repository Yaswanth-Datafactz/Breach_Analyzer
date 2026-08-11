"""Plain-text renderer. Paragraph lines are written unwrapped (one logical
line = one physical line) so {"line": n} locations are exact; Table blocks
render as pipe-separated rows (the cred-dump look).
"""

from __future__ import annotations

from pathlib import Path

from corpusgen.renderers import DocumentSpec, Paragraph, Table, planting_dict


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    lines: list[str] = [spec.title, spec.doc_date.isoformat(), ""]
    plantings: list[dict] = []

    for block in spec.blocks:
        if isinstance(block, Paragraph):
            block_lines = block.text.split("\n")
            start = len(lines)
            lines.extend(block_lines)
            for plant in block.plants:
                line_no = next(
                    (
                        start + i + 1  # 1-based
                        for i, line in enumerate(block_lines)
                        if plant.value in line
                    ),
                    None,
                )
                assert line_no is not None, f"plant {plant.value!r} not in paragraph text"
                plantings.append(planting_dict(plant, {"line": line_no}))
        else:
            assert isinstance(block, Table)
            lines.append(" | ".join(block.headers))
            row_start = len(lines)  # 0-based index of first data row
            for row in block.rows:
                lines.append(" | ".join(row))
            for row_idx, col_idx, plant in block.cell_plants:
                assert plant.value in block.rows[row_idx][col_idx]
                plantings.append(planting_dict(plant, {"line": row_start + row_idx + 1}))
        lines.append("")

    path.write_text("\n".join(lines) + "\n")
    return plantings
