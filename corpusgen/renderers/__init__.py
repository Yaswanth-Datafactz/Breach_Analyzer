"""Renderer contract (docs/plan.md §8 / D6).

A template builds a `DocumentSpec` (layout-agnostic blocks with `Plant`
markers attached to the block containing each planted value); a renderer
writes the physical file and returns the plantings with their FINAL
location filled in (page / sheet+cell / line / paragraph / table-cell /
email part / image row). Scenarios pass those straight through to the
manifest — the answer key is recorded by the code that placed the value,
never re-derived.

Determinism: PDFs are written with reportlab's invariant mode (fixed
creation date, fixed doc ID); DOCX/XLSX core properties are pinned to
FIXED_DOC_DT (their zip containers still stamp wall-clock entry times, so
byte-stability is proven on the manifest and on pdf/csv/txt/html bytes;
EML attachments are zip-normalized so attachment sha256s in the manifest
are seed-stable). Scanned-PDF degradation noise is seeded from the spec
content, never from the shared RNG stream or a wall clock.

problem_files does not register in the maps below — its outputs are
deliberately broken/mislabeled files, emitted through
`BuildContext.allocate` + `register` (scenarios.py) with an explicit
`problem` manifest entry instead of a spec->path render call.
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
    presentation: str = "prose"  # prose | table | signature | header | image


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


@dataclass
class EmailAttachment:
    """A pre-rendered file carried by an EmailSpec. `plantings` are the
    attachment content's manifest entries, already located by its own
    renderer and re-scoped to {"part": "attachment:<filename>", ...}."""

    filename: str
    content: bytes
    maintype: str
    subtype: str
    plantings: list[dict] = field(default_factory=list)


@dataclass
class EmailSpec(DocumentSpec):
    """DocumentSpec + RFC-5322 envelope. Blocks become the text/plain body
    (title is the Subject); attachments ride along as MIME parts."""

    from_name: str = ""
    from_email: str = ""
    to_name: str = ""
    to_email: str = ""
    attachments: list[EmailAttachment] = field(default_factory=list)


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
from corpusgen.renderers import (  # noqa: E402
    digital_pdf,
    docx,
    eml,
    html,
    png,
    scanned_pdf,
    txt,
    xlsx,
)

RENDERERS: dict[str, RendererFn] = {
    "digital_pdf": digital_pdf.render,
    "scanned_pdf": scanned_pdf.render,
    "docx": docx.render,
    "xlsx": xlsx.render,
    "csv": csv_renderer.render,
    "txt": txt.render,
    "html": html.render,
    "eml": eml.render,
    "png": png.render,
}

FILE_CLASS: dict[str, str] = {
    "digital_pdf": "pdf_digital",
    "scanned_pdf": "pdf_scanned",
    "docx": "docx",
    "xlsx": "xlsx",
    "csv": "csv",
    "txt": "txt",
    "html": "html",
    "eml": "eml",
    "png": "png",
}

EXTENSION: dict[str, str] = {
    "digital_pdf": ".pdf",
    "scanned_pdf": ".pdf",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "csv": ".csv",
    "txt": ".txt",
    "html": ".html",
    "eml": ".eml",
    "png": ".png",
}
