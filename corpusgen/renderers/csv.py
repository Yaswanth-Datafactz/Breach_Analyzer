"""CSV renderer (stdlib csv). Exactly one Table block per spec.
Locations: {"line": n} — 1-based over the physical file, line 1 is the
header row. QUOTE_MINIMAL keeps a planted value a contiguous substring of
its line (our values never contain double quotes), so validation can check
raw-line containment.
"""

from __future__ import annotations

import csv
from pathlib import Path

from corpusgen.renderers import DocumentSpec, Table, planting_dict


def render(spec: DocumentSpec, path: Path) -> list[dict]:
    tables = [b for b in spec.blocks if isinstance(b, Table)]
    assert len(tables) == 1 and len(spec.blocks) == 1, "csv specs are single-table"
    table = tables[0]

    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(table.headers)
        for row in table.rows:
            writer.writerow(row)

    plantings: list[dict] = []
    for row_idx, col_idx, plant in table.cell_plants:
        assert plant.value in table.rows[row_idx][col_idx]
        assert '"' not in plant.value
        plantings.append(planting_dict(plant, {"line": row_idx + 2}))
    return plantings
