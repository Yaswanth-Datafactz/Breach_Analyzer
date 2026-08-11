"""Unit tests for eml parsing (services/parsing/eml.py): body-part
passages, header passage, attachment extraction. The pipeline-level
recursion (attachment -> child document) is covered in
tests/test_pipeline_ingest.py against the real DB. No DB here."""

from __future__ import annotations

from app.services.parsing.eml import parse_eml
from tests.conftest import build_eml_bytes

_CSV_ATTACHMENT = b"Name,SSN\nCasey Vance,531-24-8817\n"


def test_body_and_header_passages():
    content = build_eml_bytes(
        body="Please find the employee export attached.\nRegards,\nDana",
        attachment_name="export.csv",
        attachment_content=_CSV_ATTACHMENT,
    )
    result = parse_eml(content)

    by_part = {p.locator["part"]: p for p in result.passages}
    assert set(by_part) == {"headers", "body"}
    assert all(p.kind == "email_part" for p in result.passages)

    assert "From: Dana Whitfield <dana.whitfield@example.org>" in by_part["headers"].text
    assert "Subject: Employee records attached" in by_part["headers"].text
    assert "employee export attached" in by_part["body"].text
    assert by_part["body"].locator["content_type"] == "text/plain"


def test_html_alternative_body_is_tag_stripped():
    content = build_eml_bytes(
        body="plain version",
        html_body="<html><body><p>HTML version with <b>SSN 531-24-8817</b></p></body></html>",
        attachment_name="export.csv",
        attachment_content=_CSV_ATTACHMENT,
    )
    result = parse_eml(content)

    bodies = [p for p in result.passages if p.locator["part"] == "body"]
    assert len(bodies) == 2
    html_bodies = [p for p in bodies if p.locator["content_type"] == "text/html"]
    assert len(html_bodies) == 1
    assert "SSN 531-24-8817" in html_bodies[0].text
    assert "<b>" not in html_bodies[0].text
    # walk order is stable, so body indices are deterministic.
    assert sorted(p.locator["index"] for p in bodies) == [1, 2]


def test_attachment_is_extracted_not_parsed_inline():
    content = build_eml_bytes(
        body="see attachment",
        attachment_name="export.csv",
        attachment_content=_CSV_ATTACHMENT,
    )
    result = parse_eml(content)

    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.filename == "export.csv"
    assert attachment.content == _CSV_ATTACHMENT
    # The attachment's content contributed NO passage -- it becomes a child
    # document via the pipeline, never inline text.
    assert not any("Casey Vance" in p.text for p in result.passages)
