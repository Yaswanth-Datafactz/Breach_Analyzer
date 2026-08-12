"""Chunker tests (docs/plan.md §9 + §14b): docx aggregation respects
locators and never mixes locator kinds, the xlsx header-map path collapses
the 80-person sheet to a single tier-1 sample chunk, low-OCR image passages
route to the vision kind, and the chunk->passage span map round-trips
extracted values to passage-local offsets. Pure functions -- no DB, no
adapters, no model calls."""

from __future__ import annotations

import uuid

from app.db.models import Passage
from app.services.extraction import chunker
from app.services.extraction.schemas import ElementOut, ElementType
from app.services.parsing.xlsx import build_header_map

DOC_ID = uuid.uuid4()


def _passage(seq: int, kind: str, locator: dict, text: str, **kwargs) -> Passage:
    return Passage(
        id=uuid.uuid4(), document_id=DOC_ID, seq=seq, kind=kind, locator=locator,
        text=text, ocr=kwargs.get("ocr", False), page_image_sha=kwargs.get("page_image_sha"),
    )


def _build(passages, *, max_tokens=3000, sample_rows=5, vision_threshold=60.0):
    return chunker.build_chunks(
        passages,
        max_tokens=max_tokens,
        sheet_sample_rows=sample_rows,
        vision_conf_threshold=vision_threshold,
    )


# --- docx aggregation (§14b binding: locators, never seq across kinds) ------


def test_docx_paragraphs_aggregate_but_never_mix_with_tables():
    # python-docx yields paragraphs before tables, so by seq the table is
    # LAST -- but a run built on seq adjacency across kinds would happily
    # glue paragraph 3 to the table. Locator families must keep them apart.
    passages = [
        _passage(0, "text_block", {"paragraph": 1}, "Termination memo for Robert Fenwick."),
        _passage(1, "text_block", {"paragraph": 2}, "Effective date March 3, 2026."),
        _passage(2, "text_block", {"paragraph": 4}, "HR retains the personnel file."),
        _passage(3, "text_block", {"table": 1, "row_start": 1, "row_end": 2}, "Field | Value\nSSN | 523-41-8722"),
    ]
    chunks = _build(passages)

    assert all(c.kind == chunker.TEXT for c in chunks)
    paragraph_chunks = [
        c for c in chunks if all("paragraph" in s.passage.locator for s in c.spans)
    ]
    table_chunks = [c for c in chunks if any("table" in s.passage.locator for s in c.spans)]
    assert len(paragraph_chunks) == 1 and len(table_chunks) == 1
    # No chunk mixes the two locator kinds.
    for chunk in chunks:
        kinds = {("table" if "table" in s.passage.locator else "paragraph") for s in chunk.spans}
        assert len(kinds) == 1
    # Paragraph order follows the locator (1, 2, 4), and the chunk text is a
    # verbatim concatenation.
    spans = paragraph_chunks[0].spans
    assert [s.passage.locator["paragraph"] for s in spans] == [1, 2, 4]
    for span in spans:
        assert paragraph_chunks[0].text[span.chunk_start : span.chunk_end] == span.passage.text


def test_docx_paragraphs_shuffled_input_still_orders_by_locator():
    ordered_texts = [f"Paragraph number {n} body." for n in (1, 2, 3)]
    passages = [
        _passage(2, "text_block", {"paragraph": 3}, ordered_texts[2]),
        _passage(0, "text_block", {"paragraph": 1}, ordered_texts[0]),
        _passage(1, "text_block", {"paragraph": 2}, ordered_texts[1]),
    ]
    (chunk,) = _build(passages)
    assert chunk.text == "\n\n".join(ordered_texts)


def test_token_budget_splits_paragraph_runs():
    body = "x" * 4000  # ~1000 tokens each
    passages = [
        _passage(i, "text_block", {"paragraph": i + 1}, f"p{i + 1} {body}") for i in range(5)
    ]
    chunks = _build(passages, max_tokens=2500)
    assert len(chunks) > 1
    # Every chunk stays a contiguous, ordered slice of the paragraph run.
    seen = [s.passage.locator["paragraph"] for c in chunks for s in c.spans]
    assert seen == sorted(seen) == [1, 2, 3, 4, 5]


def test_oversized_single_passage_is_never_split():
    passages = [_passage(0, "page", {"page": 1}, "y" * 20_000)]
    (chunk,) = _build(passages, max_tokens=1000)
    assert chunk.text == passages[0].text  # own chunk, intact


# --- xlsx header-map path (§9: the 80-person sheet ~ ONE tier-1 call) -------


HEADERS = ["Name", "SSN", "DOB", "Email", "Notes"]


def _sheet_passages(rows_total: int = 80, per_passage: int = 40) -> list[Passage]:
    header_map = build_header_map(HEADERS)
    # Sanity: Notes is the one unmapped column of the fixture.
    assert [e["element_type"] for e in header_map] == ["name", "ssn", "dob", "email", None]
    passages = []
    header_line = " | ".join(HEADERS)
    for start in range(0, rows_total, per_passage):
        lines = [header_line]
        for i in range(start, min(start + per_passage, rows_total)):
            lines.append(
                " | ".join(
                    [
                        f"Person {i} Smith",
                        f"523-41-{1000 + i:04d}",
                        "01/15/1980",
                        f"person{i}@example.com",
                        f"Diagnosed with condition {i}",
                    ]
                )
            )
        passages.append(
            _passage(
                len(passages),
                "sheet_range",
                {
                    "sheet": "Employees",
                    "row_start": start + 2,
                    "row_end": start + 1 + len(lines) - 1,
                    "header_map": header_map,
                },
                "\n".join(lines),
            )
        )
    return passages


def test_eighty_person_sheet_costs_one_sample_chunk():
    chunks = _build(_sheet_passages())
    assert len(chunks) == 1
    (chunk,) = chunks
    assert chunk.kind == chunker.SHEET_SAMPLE
    assert not chunk.verbatim
    assert chunk.sheet == "Employees"
    assert chunk.unmapped_columns == (5,)
    assert chunk.sampled_row_numbers == (2, 3, 4, 5, 6)
    assert len(chunk.spans) == 2  # both 40-row passages resolvable
    # The model sees ONLY the unmapped column: no mapped values leak in.
    assert "Diagnosed with condition 0" in chunk.text
    assert "523-41-1000" not in chunk.text
    assert "person0@example.com" not in chunk.text
    assert "Person 0 Smith" not in chunk.text


def test_fully_mapped_sheet_produces_zero_llm_chunks():
    header_map = build_header_map(["Name", "SSN"])
    passage = _passage(
        0,
        "sheet_range",
        {"sheet": "S", "row_start": 2, "row_end": 3, "header_map": header_map},
        "Name | SSN\nCasey Vance | 531-24-8817",
    )
    assert _build([passage]) == []


# --- vision routing (§14b R1) ------------------------------------------------


def test_low_ocr_confidence_image_routes_to_vision_not_text():
    garbage = _passage(
        0, "page", {"page": 1, "ocr_mean_conf": 52.0}, "j8#(f ksd8 3l1x",
        ocr=True, page_image_sha="a" * 64,
    )
    healthy = _passage(
        1, "page", {"page": 1, "ocr_mean_conf": 88.5}, "A clean scanned memo body.",
        ocr=True, page_image_sha="b" * 64,
    )
    chunks = _build([garbage, healthy])
    kinds = {c.kind for c in chunks}
    assert kinds == {chunker.VISION, chunker.TEXT}
    vision = next(c for c in chunks if c.kind == chunker.VISION)
    assert vision.spans[0].passage is garbage


# --- span-map round trip ------------------------------------------------------


def _element(element_type: ElementType, value: str) -> ElementOut:
    return ElementOut(element_type=element_type, value_raw=value, mention_ref=None, confidence=0.9)


def test_span_map_round_trips_to_passage_local_offsets():
    passages = [
        _passage(0, "text_block", {"paragraph": 1}, "Intro paragraph, nothing planted."),
        _passage(1, "text_block", {"paragraph": 2}, "Member SSN 531-24-8817 on file."),
        _passage(2, "text_block", {"paragraph": 3}, "Duplicate mention of 531-24-8817 here."),
    ]
    (chunk,) = _build(passages)
    elements = [
        _element(ElementType.SSN, "531-24-8817"),
        _element(ElementType.SSN, "531-24-8817"),  # occurrence order -> passage 3
    ]
    first, second = chunker.locate_in_chunk(chunk, elements)

    assert first.passage is passages[1]
    assert first.passage.text[first.char_start : first.char_end] == "531-24-8817"
    assert second.passage is passages[2]
    assert second.passage.text[second.char_start : second.char_end] == "531-24-8817"


def test_unlocatable_value_flags_offsets_missing():
    passages = [_passage(0, "text_block", {"paragraph": 1}, "Nothing to see.")]
    (chunk,) = _build(passages)
    (located,) = chunker.locate_in_chunk(chunk, [_element(ElementType.EMAIL, "ghost@nowhere.io")])
    assert located.offsets_missing
    assert located.passage is None


def test_sheet_sample_chunk_locates_against_passage_text():
    passages = _sheet_passages()
    (chunk,) = _build(passages)
    # A value from row 50 lives in the SECOND passage; the non-verbatim
    # locator must find it there.
    (located,) = chunker.locate_in_chunk(
        chunk, [_element(ElementType.MEDICAL, "Diagnosed with condition 49")]
    )
    assert located.passage is passages[1]
    assert (
        located.passage.text[located.char_start : located.char_end]
        == "Diagnosed with condition 49"
    )
