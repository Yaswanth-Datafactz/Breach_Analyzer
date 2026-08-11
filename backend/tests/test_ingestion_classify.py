"""Unit tests for ingest-time routing (services/ingestion/classify.py):
each plan §8 problem kind must land on its expected reason_code -- or, for
the recoverable wrong-extension trap, on a quarantine-free reclassified
route (the deterministic recovery the task hardened; docs/plan.md's
boundary rule: enumerable control flow never reaches an agent). Pure
functions, no DB."""

from __future__ import annotations

from app.services.ingestion import classify
from app.services.ingestion.inventory import sniff_mime
from tests.conftest import build_eml_bytes, build_password_pdf_bytes, build_xlsx_bytes


def _route(filename: str, content: bytes) -> classify.RouteDecision:
    from app.services.ingestion.inventory import declared_mime_for

    return classify.route(
        filename=filename,
        byte_size=len(content),
        declared_mime=declared_mime_for(filename),
        sniffed_mime=sniff_mime(content),
        content=content,
    )


def test_zero_byte_quarantines():
    decision = _route("empty.txt", b"")
    assert decision.quarantine_reason == "zero_byte"
    assert decision.parser_id is None


def test_password_protected_pdf_quarantines():
    decision = _route("secret.pdf", build_password_pdf_bytes())
    assert decision.quarantine_reason == "password_protected"
    assert decision.file_class == "pdf_digital"


def test_corrupt_pdf_quarantines_at_classify():
    # Sniffs as PDF (magic header intact) but pikepdf cannot open it.
    decision = _route("broken.pdf", b"%PDF-1.7 not really a pdf body")
    assert decision.quarantine_reason == "corrupt"


def test_wrong_extension_with_unambiguous_sniff_is_recovered_not_quarantined():
    # An xlsx renamed .pdf: routed by its REAL type, with the reclassified
    # note in the sniffed_mime the pipeline stores.
    decision = _route("actually_xlsx.pdf", build_xlsx_bytes(["Name"], [["Casey Vance"]]))
    assert decision.quarantine_reason is None
    assert decision.parser_id == "xlsx"
    assert decision.file_class == "xlsx"
    assert decision.reclassified is True
    assert decision.sniffed_mime_note is not None
    assert "reclassified-from=pdf_digital" in decision.sniffed_mime_note


def test_wrong_extension_with_disproven_claim_quarantines():
    # Extension claims docx; bytes are unrecognizable junk -- unrecoverable.
    decision = _route("binary_junk.docx", b"\x00\xffJUNKDATA" * 64)
    assert decision.quarantine_reason == "wrong_extension"
    assert decision.parser_id is None


def test_unknown_binary_quarantines_as_unsupported():
    decision = _route("noise.bin", b"\x00\xffJUNKDATA" * 64)
    assert decision.quarantine_reason == "unsupported"


def test_msg_quarantines_as_unsupported_cut_line():
    # OLE compound-file magic bytes: what a real .msg sniffs as.
    content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 1024
    decision = _route("outlook_item.msg", content)
    assert decision.file_class == "msg"
    assert decision.quarantine_reason == "unsupported"


def test_eml_routes_by_sniff():
    content = build_eml_bytes(
        body="plain body", attachment_name="a.csv", attachment_content=b"h\n1\n"
    )
    decision = _route("message.eml", content)
    assert decision.parser_id == "eml"
    assert decision.file_class == "eml"
    assert decision.quarantine_reason is None


def test_text_family_resolves_by_extension():
    # Headerless prose sniffs text/plain; within the text family the
    # extension is compatible information, not a conflict.
    assert _route("notes.csv", b"just some words\n").parser_id == "csv"
    assert _route("notes.txt", b"just some words\n").parser_id == "txt"


def test_truncated_docx_still_routes_to_docx_parser():
    # A truncated docx keeps its zip header, so the sniff still says docx --
    # corruption surfaces at PARSE time (pipeline maps it to `corrupt`);
    # classify must route it, not guess.
    from tests.conftest import build_docx_bytes

    decision = _route("truncated.docx", build_docx_bytes(["will be cut"])[:400])
    assert decision.parser_id == "docx"
    assert decision.quarantine_reason is None
