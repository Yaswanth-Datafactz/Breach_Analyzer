"""Ingest-time routing (docs/plan.md §3's INGEST step, second half):
file_class -> parser id, or a quarantine reason -- decided from the SNIFFED
type, never the extension.

The wrong-extension trap (plan §8's problem_files) is deliberately
RECOVERABLE here: when the sniff is unambiguous and the sniffed type has a
parser (an xlsx renamed `.pdf`), the file is routed by its real type with a
`; reclassified=...` note appended to documents.sniffed_mime -- no
quarantine, no agent, zero cost (plan's boundary rule: enumerable control
flow stays in code). `wrong_extension` as a quarantine reason is reserved
for the unrecoverable variant: the extension claims a parseable type but
the bytes are unrecognizable, so the claim is provably false and nothing
deterministic can be done with the content.

Password-protected PDFs are caught here (pikepdf open attempt -- cheap,
and catching them at ingest means they never burn a parse slot); corrupt
files that still sniff as their real type (a truncated docx keeps its zip
header) surface at parse time instead, mapped to `corrupt` by the pipeline's
per-document exception boundary (services/pipeline.py).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf

# file_class -> parser id (services/pipeline.py dispatches parser ids to the
# parsing modules). A class absent here has no deterministic parser: `msg`
# is the plan's first cut-line (§14 -- EML alone keeps 6+ types), `unknown`
# is by definition unroutable.
PARSER_BY_CLASS: dict[str, str] = {
    "pdf_digital": "pdf",
    "pdf_scanned": "pdf",  # the digital/scanned split is decided at parse time
    "docx": "docx",
    "xlsx": "xlsx",
    "csv": "csv",
    "eml": "eml",
    "html": "html",
    "txt": "txt",
    "png": "png",
}

# Unambiguous sniffed MIME -> file_class. PDFs enter as pdf_digital; the
# parse-stage OCR heuristic flips the class to pdf_scanned when the pages
# turn out to be image-based (documents.is_image_based is set at the same
# moment -- never guessed from the filename).
_MIME_TO_CLASS: dict[str, str] = {
    "application/pdf": "pdf_digital",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv",
    "application/csv": "csv",
    "text/html": "html",
    "message/rfc822": "eml",
    "image/png": "png",
    "application/vnd.ms-outlook": "msg",
    "application/x-ole-storage": "msg",
    "application/CDFV2": "msg",
}

# The text/plain family: libmagic reports plain text for content it cannot
# subtype (a headerless CSV, a minimal eml, ordinary txt), so within this
# family the extension is allowed to pick the parser -- the sniff and the
# claim are compatible, not conflicting.
_TEXT_FAMILY_BY_EXTENSION: dict[str, str] = {
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".eml": "eml",
    ".txt": "txt",
}

_EXTENSION_TO_CLASS: dict[str, str] = {
    ".pdf": "pdf_digital",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".csv": "csv",
    ".eml": "eml",
    ".msg": "msg",
    ".html": "html",
    ".htm": "html",
    ".txt": "txt",
    ".png": "png",
}


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of ingest-time classification for one file.

    Either `parser_id` is set (route to that parser) or `quarantine_reason`
    is (plan §4's reason_code enum) -- never both. `reclassified` means the
    extension lied but the sniff was unambiguous, and `sniffed_mime_note`
    carries the annotated value the pipeline stores in
    documents.sniffed_mime (MIME parameter syntax, so
    `.split(";")[0]` still yields a servable media type)."""

    file_class: str
    parser_id: str | None = None
    quarantine_reason: str | None = None
    detail: str | None = None
    reclassified: bool = False
    sniffed_mime_note: str | None = None


def _class_from_sniff(sniffed_mime: str, filename: str, content: bytes) -> str:
    if sniffed_mime in _MIME_TO_CLASS:
        return _MIME_TO_CLASS[sniffed_mime]
    if sniffed_mime == "text/plain":
        return _TEXT_FAMILY_BY_EXTENSION.get(Path(filename).suffix.lower(), "txt")
    if sniffed_mime == "application/zip":
        # An OOXML container whose [Content_Types].xml libmagic couldn't
        # reach still identifies itself by its member paths.
        try:
            names = zipfile.ZipFile(io.BytesIO(content)).namelist()
        except zipfile.BadZipFile:
            # Zip header present but the archive is unreadable -- a
            # truncated docx/xlsx. If the extension claims an OOXML class,
            # route it there so the PARSER's failure produces the honest
            # `corrupt` quarantine (task rule: corrupt via parse failure);
            # anything else is unroutable.
            declared = _EXTENSION_TO_CLASS.get(Path(filename).suffix.lower())
            return declared if declared in ("docx", "xlsx") else "unknown"
        if any(name.startswith("word/") for name in names):
            return "docx"
        if any(name.startswith("xl/") for name in names):
            return "xlsx"
        return "unknown"
    return "unknown"


def _probe_pdf(content: bytes) -> tuple[str, str] | None:
    """pikepdf open attempt (the task-specified detector for encrypted
    PDFs). Returns (reason_code, detail) when the PDF cannot be parsed at
    all, None when it is parseable."""
    try:
        with pikepdf.open(io.BytesIO(content)):
            return None
    except pikepdf.PasswordError:
        return "password_protected", "pikepdf: file is encrypted and no password is available"
    except pikepdf.PdfError as exc:
        return "corrupt", f"pikepdf: {exc}"


def route(
    *,
    filename: str,
    byte_size: int,
    declared_mime: str | None,
    sniffed_mime: str,
    content: bytes,
) -> RouteDecision:
    """Classify one inventoried file into a parser route or a quarantine.

    Precedence: zero-byte first (nothing else is knowable about an empty
    file), then the sniffed type (routing by content, with the wrong-
    extension recovery when it disagrees with the extension), then the
    unrecoverable cases (`wrong_extension` when the extension's claim is
    disproven, `unsupported` when nothing claims a parseable type)."""
    declared_class = _EXTENSION_TO_CLASS.get(Path(filename).suffix.lower(), "unknown")

    if byte_size == 0:
        return RouteDecision(
            file_class=declared_class,
            quarantine_reason="zero_byte",
            detail="file is empty (0 bytes)",
        )

    sniffed_class = _class_from_sniff(sniffed_mime, filename, content)

    if sniffed_class in PARSER_BY_CLASS:
        decision_class = sniffed_class
        reclassified = declared_class != sniffed_class and not (
            declared_class.startswith("pdf") and sniffed_class.startswith("pdf")
        )
        sniffed_mime_note = None
        if reclassified:
            sniffed_mime_note = (
                f"{sniffed_mime}; reclassified-from={declared_class}"
            )

        if sniffed_class in ("pdf_digital", "pdf_scanned"):
            probe = _probe_pdf(content)
            if probe is not None:
                reason, detail = probe
                return RouteDecision(
                    file_class=decision_class,
                    quarantine_reason=reason,
                    detail=detail,
                    reclassified=reclassified,
                    sniffed_mime_note=sniffed_mime_note,
                )

        return RouteDecision(
            file_class=decision_class,
            parser_id=PARSER_BY_CLASS[sniffed_class],
            reclassified=reclassified,
            sniffed_mime_note=sniffed_mime_note,
        )

    if sniffed_class == "msg":
        return RouteDecision(
            file_class="msg",
            quarantine_reason="unsupported",
            detail="MSG parsing is the plan's first cut-line (docs/plan.md §14); "
            "no deterministic parser is registered",
        )

    if declared_class in PARSER_BY_CLASS:
        # The extension claims a type we could parse, but the bytes are
        # unrecognizable -- the claim is disproven, not merely different.
        return RouteDecision(
            file_class="unknown",
            quarantine_reason="wrong_extension",
            detail=f"extension claims {declared_class} but content sniffs as "
            f"{sniffed_mime}, which no parser accepts",
        )

    return RouteDecision(
        file_class="unknown",
        quarantine_reason="unsupported",
        detail=f"no parser for sniffed type {sniffed_mime} "
        f"(declared {declared_mime or 'unknown'})",
    )
