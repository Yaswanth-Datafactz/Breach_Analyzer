"""Problem-file builders (docs/plan.md §8 ProblemFiles): each writes one
deliberately broken or mislabeled file and returns (plantings, problem)
where `problem` is the manifest contract the exception-investigator
fixtures are built from:

    {kind, expected_reason_code (docs/plan.md §4 quarantines enum),
     recoverable: bool, recovery_hint, ...kind-specific extras}

Recoverable kinds (password_pdf, xlsx_as_pdf, docx_as_txt, png_as_xlsx)
keep their real plantings — once the investigator re-routes the file, the
answer key applies; unrecoverable kinds (truncated_pdf, zero_byte) record
none, and truncated_pdf asserts no planted value survives in the retained
byte prefix so ground truth cannot leak into the wreckage.

These do not register in RENDERERS: the ProblemFiles scenario names each
output file itself (the wrong extension IS the fixture) and attaches the
problem dict via BuildContext.register.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pikepdf

from corpusgen.renderers import DocumentSpec, digital_pdf, docx, png, xlsx
from corpusgen.renderers.eml import normalize_zip_bytes

# Keep the corrupt file to a short head: reportlab's uncompressed page
# streams put planted values in cleartext, so we cut BEFORE the first
# content stream (and assert it) — the truncated wreck must contain no
# recoverable ground truth.
_TRUNCATE_AT = 600


def password_pdf(
    spec: DocumentSpec, path: Path, password: str
) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        clear_path = Path(tmp) / "clear.pdf"
        plantings = digital_pdf.render(spec, clear_path)
        with pikepdf.open(clear_path) as pdf:
            pdf.save(
                path,
                encryption=pikepdf.Encryption(owner=password, user=password, R=6),
            )
    problem = {
        "kind": "password_pdf",
        "expected_reason_code": "password_protected",
        "recoverable": True,
        "recovery_hint": "open with the user password recorded here",
        "password": password,
    }
    return plantings, problem


def truncated_pdf(spec: DocumentSpec, path: Path) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory() as tmp:
        clear_path = Path(tmp) / "clear.pdf"
        plantings = digital_pdf.render(spec, clear_path)
        data = clear_path.read_bytes()
    head = data[:_TRUNCATE_AT]
    assert head.startswith(b"%PDF") and b"%%EOF" not in head
    for plant in plantings:
        assert plant["value"].encode() not in head, "planting leaked into wreckage"
    path.write_bytes(head)
    problem = {
        "kind": "truncated_pdf",
        "expected_reason_code": "corrupt",
        "recoverable": False,
        "recovery_hint": "no xref/trailer; request a re-export from the source",
        "original_byte_size": len(data),
    }
    return [], problem


def zero_byte(path: Path) -> tuple[list[dict], dict]:
    path.write_bytes(b"")
    problem = {
        "kind": "zero_byte",
        "expected_reason_code": "zero_byte",
        "recoverable": False,
        "recovery_hint": "empty upload; request a re-export from the source",
    }
    return [], problem


def xlsx_as_pdf(spec: DocumentSpec, path: Path) -> tuple[list[dict], dict]:
    """Real xlsx bytes under a .pdf name — sniff-vs-extension fixture."""
    with tempfile.TemporaryDirectory() as tmp:
        true_path = Path(tmp) / "true.xlsx"
        plantings = xlsx.render(spec, true_path)
        path.write_bytes(normalize_zip_bytes(true_path.read_bytes()))
    problem = {
        "kind": "xlsx_as_pdf",
        "expected_reason_code": "wrong_extension",
        "recoverable": True,
        "recovery_hint": "content sniffs as OOXML zip; parse as xlsx",
        "true_class": "xlsx",
        "declared_extension": ".pdf",
    }
    return plantings, problem


def docx_as_txt(spec: DocumentSpec, path: Path) -> tuple[list[dict], dict]:
    """Real docx bytes under a .txt name."""
    with tempfile.TemporaryDirectory() as tmp:
        true_path = Path(tmp) / "true.docx"
        plantings = docx.render(spec, true_path)
        path.write_bytes(normalize_zip_bytes(true_path.read_bytes()))
    problem = {
        "kind": "docx_as_txt",
        "expected_reason_code": "wrong_extension",
        "recoverable": True,
        "recovery_hint": "content sniffs as OOXML zip; parse as docx",
        "true_class": "docx",
        "declared_extension": ".txt",
    }
    return plantings, problem


def png_as_xlsx(spec: DocumentSpec, path: Path) -> tuple[list[dict], dict]:
    """Screenshot-of-spreadsheet PNG bytes under a .xlsx name — fails the
    spreadsheet parser, sniffs as an image, recovered via OCR."""
    with tempfile.TemporaryDirectory() as tmp:
        true_path = Path(tmp) / "true.png"
        plantings = png.render(spec, true_path)
        path.write_bytes(true_path.read_bytes())
    problem = {
        "kind": "png_as_xlsx",
        "expected_reason_code": "wrong_extension",
        "recoverable": True,
        "recovery_hint": "content sniffs as PNG; route to OCR",
        "true_class": "png",
        "declared_extension": ".xlsx",
    }
    return plantings, problem
