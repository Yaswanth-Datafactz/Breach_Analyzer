"""Unit tests for xlsx passage chunking and the deterministic header-
lexicon hook (services/parsing/xlsx.py). No DB."""

from __future__ import annotations

from app.services.parsing.xlsx import ROWS_PER_PASSAGE, map_header, parse_xlsx
from tests.conftest import build_xlsx_bytes

_HEADERS = ["Name", "SSN", "DOB", "Email", "Phone", "Account"]


def _rows(count: int) -> list[list[str]]:
    return [
        [
            f"Person {i}",
            f"523-{i % 90 + 10:02d}-{1000 + i}",
            "1990-01-01",
            f"person{i}@example.org",
            "(802) 555-0100",
            str(60000000000 + i),
        ]
        for i in range(count)
    ]


def test_chunking_row_ranges_are_real_spreadsheet_rows():
    content = build_xlsx_bytes(_HEADERS, _rows(85), sheet="Customers")
    result = parse_xlsx(content)

    assert len(result.passages) == 3  # ceil(85 / 40)
    ranges = [(p.locator["row_start"], p.locator["row_end"]) for p in result.passages]
    # Data starts at spreadsheet row 2 (row 1 = headers, corpusgen's convention).
    assert ranges == [(2, 41), (42, 81), (82, 86)]
    assert all(p.kind == "sheet_range" for p in result.passages)
    assert all(p.locator["sheet"] == "Customers" for p in result.passages)


def test_every_chunk_repeats_the_header_line_and_keeps_values_verbatim():
    rows = _rows(ROWS_PER_PASSAGE + 5)
    content = build_xlsx_bytes(_HEADERS, rows, sheet="Customers")
    result = parse_xlsx(content)

    for passage in result.passages:
        assert passage.text.splitlines()[0] == " | ".join(_HEADERS)
    # A value from the second chunk's rows is findable byte-exact there
    # (the D9 rule: offsets come from passage.find(value_raw)).
    last_row_ssn = rows[-1][1]
    assert last_row_ssn in result.passages[-1].text
    assert last_row_ssn not in result.passages[0].text


def test_header_map_is_emitted_into_every_locator():
    content = build_xlsx_bytes(_HEADERS, _rows(3), sheet="Customers")
    result = parse_xlsx(content)

    header_map = result.passages[0].locator["header_map"]
    mapped = {entry["header"]: entry["element_type"] for entry in header_map}
    assert mapped == {
        "Name": "name",
        "SSN": "ssn",
        "DOB": "dob",
        "Email": "email",
        "Phone": "phone",
        "Account": "financial_account",
    }
    letters = [entry["letter"] for entry in header_map]
    assert letters == ["A", "B", "C", "D", "E", "F"]


def test_header_lexicon_is_exact_match_and_abstains():
    assert map_header("Social Security Number") == "ssn"
    assert map_header(" Date_of_Birth ") == "dob"
    assert map_header("password") == "credential"
    # Abstention, not guessing: unknown headers stay None for the LLM path.
    assert map_header("employee_id") is None
    assert map_header("Favorite Color") is None


def test_unmapped_headers_get_none_in_header_map():
    content = build_xlsx_bytes(["employee_id", "username", "password", "ssn"], [["E100", "cvance", "hunter2", "531-24-8817"]], sheet="creds")
    result = parse_xlsx(content)
    mapped = {e["header"]: e["element_type"] for e in result.passages[0].locator["header_map"]}
    assert mapped == {
        "employee_id": None,
        "username": "credential",
        "password": "credential",
        "ssn": "ssn",
    }
