"""Renderer contract (docs/plan.md §8 / D6).

A template builds a `DocumentSpec` (layout-agnostic blocks with `Plant`
markers attached to the block containing each planted value); a renderer
writes the physical file and returns the plantings with their FINAL
location filled in (page / sheet+cell / line / paragraph / table-cell).
Scenarios pass those straight through to the manifest — the answer key is
recorded by the code that placed the value, never re-derived.

Determinism: PDFs are written with reportlab's invariant mode (fixed
creation date, fixed doc ID); DOCX/XLSX core properties are pinned to
FIXED_DOC_DT (their zip containers still stamp wall-clock entry times, so
byte-stability is proven on the manifest and on pdf/csv/txt/html bytes).

Extension points (tomorrow): scanned_pdf, eml, png, problem_files register
here exactly like the digital six — add the module, extend the three maps.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Union

FIXED_DOC_DT = dt.datetime(2026, 1, 15, 12, 0, 0)


@dataclass
class Plant:
    """A value the manifest must account for. person_uid is None for traps."""

    person_uid: str | None
    element_type: str
    value: str
    presentation: str = "prose"  # prose | table | signature


@dataclass
class Paragraph:
    text: str  # may contain \n — renderers treat each line as a unit
    plants: list[Plant] = field(default_factory=list)


@dataclass
class Table:
    name: str  # sheet name for xlsx; ignored elsewhere
    headers: list[str]
    rows: list[list[str]]
    # (data_row_idx, col_idx, plant) — 0-based over `rows`
    cell_plants: list[tuple[int, int, Plant]] = field(default_factory=list)


Block = Union[Paragraph, Table]


@dataclass
class DocumentSpec:
    archetype: str
    title: str
    doc_date: dt.date
    blocks: list[Block]


def planting_dict(plant: Plant, location: dict) -> dict:
    return {
        "person_uid": plant.person_uid,
        "element_type": plant.element_type,
        "value": plant.value,
        "location": location,
        "presentation": plant.presentation,
    }


RendererFn = Callable[[DocumentSpec, Path], list[dict]]

from corpusgen.renderers import csv as csv_renderer  # noqa: E402
from corpusgen.renderers import digital_pdf, docx, html, txt, xlsx  # noqa: E402

RENDERERS: dict[str, RendererFn] = {
    "digital_pdf": digital_pdf.render,
    "docx": docx.render,
    "xlsx": xlsx.render,
    "csv": csv_renderer.render,
    "txt": txt.render,
    "html": html.render,
}

FILE_CLASS: dict[str, str] = {
    "digital_pdf": "pdf_digital",
    "docx": "docx",
    "xlsx": "xlsx",
    "csv": "csv",
    "txt": "txt",
    "html": "html",
}

EXTENSION: dict[str, str] = {
    "digital_pdf": ".pdf",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "csv": ".csv",
    "txt": ".txt",
    "html": ".html",
}
