"""Shared fixtures for the B1 ingestion/parsing/pipeline tests.

Problem-file fixtures are BUILT here, deterministically, into
tests/fixtures/ -- handcrafted so these tests never depend on the parallel
corpusgen track's problem_files renderer timing (docs/plan.md §14 runs the
tracks concurrently). Each is the smallest possible instance of one plan §8
problem kind.

DB-touching tests follow UC2's convention (see tests/test_models_smoke.py):
they run against the real docker-compose Postgres on :5434 with migrations
applied. Because documents.sha256 is globally UNIQUE, tests that ingest
fixed-content fixtures first free any colliding rows left by earlier test
sessions (`free_sha_collisions`) -- deleting by sha cascades passages/
quarantines via the schema's ondelete rules.
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from docx import Document as DocxDocument
from openpyxl import Workbook
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.models import Document

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def build_password_pdf_bytes() -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    buffer = io.BytesIO()
    pdf.save(buffer, encryption=pikepdf.Encryption(owner="owner-pass", user="user-pass", R=6))
    return buffer.getvalue()


def build_docx_bytes(lines: list[str]) -> bytes:
    doc = DocxDocument()
    for line in lines:
        doc.add_paragraph(line)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def build_xlsx_bytes(headers: list[str], rows: list[list[str]], sheet: str = "Sheet1") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_eml_bytes(
    *, body: str, attachment_name: str, attachment_content: bytes, html_body: str | None = None
) -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "Dana Whitfield <dana.whitfield@example.org>"
    message["To"] = "hr-intake@example.org"
    message["Subject"] = "Employee records attached"
    message["Date"] = "Mon, 15 Jan 2026 12:00:00 -0000"
    message.set_content(body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    message.add_attachment(
        attachment_content,
        maintype="text",
        subtype="csv",
        filename=attachment_name,
    )
    return message.as_bytes()


# The six handcrafted problem kinds (plan §8's problem_files, minus the
# scan-specific ones): filename -> byte builder. actually_xlsx.pdf is the
# RECOVERABLE wrong-extension trap (routes to the xlsx parser, no
# quarantine); binary_junk.docx is the unrecoverable one (extension claims
# docx, bytes disprove it).
def _problem_file_builders() -> dict[str, bytes]:
    return {
        "password_protected.pdf": build_password_pdf_bytes(),
        "truncated.docx": build_docx_bytes(["This document will be cut off mid-zip."])[: 400],
        "zero_byte.txt": b"",
        "actually_xlsx.pdf": build_xlsx_bytes(["Name", "SSN"], [["Casey Vance", "531-24-8817"]]),
        # Distinct byte patterns: identical content would sha256-dedup into
        # ONE documents row and break the per-file quarantine assertions.
        "binary_junk.docx": b"\x00\xffJUNKDATA" * 64,
        "noise.bin": b"\x01\xfeNOISEBIN" * 64,
    }


@pytest.fixture(scope="session")
def problem_files() -> dict[str, Path]:
    """Materializes the problem fixtures under tests/fixtures/ and returns
    filename -> path. Rebuilt every session (cheap, and the encrypted PDF's
    salts make its bytes fresh each time anyway)."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for filename, content in _problem_file_builders().items():
        path = FIXTURES_DIR / filename
        path.write_bytes(content)
        paths[filename] = path
    return paths


def free_sha_collisions(db: Session, contents: list[bytes]) -> None:
    """Delete documents rows (cascading passages/quarantines) whose sha256
    matches any of `contents` -- documents.sha256 is globally UNIQUE, so a
    prior test session's rows would otherwise shadow this session's ingest
    as dedup hits."""
    from app.services.ingestion.inventory import sha256_of

    shas = [sha256_of(content) for content in contents]
    db.execute(delete(Document).where(Document.sha256.in_(shas)))
    db.commit()
