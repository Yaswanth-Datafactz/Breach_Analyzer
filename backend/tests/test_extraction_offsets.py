"""Unit tests for service-computed offsets (docs/plan.md D9: model
offsets measured 0/10 -> the service string-matches value_raw instead;
occurrence order disambiguates duplicates; no match -> offsets_missing,
routed to review rather than guessed)."""

from app.services.extraction.offsets import LocatedElement, locate_values
from app.services.extraction.schemas import ElementOut, ElementType


def _element(value_raw: str, element_type: ElementType = ElementType.SSN) -> ElementOut:
    return ElementOut(element_type=element_type, value_raw=value_raw, confidence=0.9)


def test_single_value_located_exactly():
    text = "Payroll currently lists the Social Security number 523-41-8722 for Maria."
    (located,) = locate_values(text, [_element("523-41-8722")])
    assert not located.offsets_missing
    assert (located.char_start, located.char_end) == (51, 62)
    assert text[located.char_start : located.char_end] == "523-41-8722"


def test_multiple_distinct_values_preserve_input_order():
    text = "SSN 523-41-8722; email maria.alvarez12@example.net on file."
    elements = [
        _element("maria.alvarez12@example.net", ElementType.EMAIL),
        _element("523-41-8722"),
    ]
    located = locate_values(text, elements)
    assert [loc.element.value_raw for loc in located] == [
        "maria.alvarez12@example.net",
        "523-41-8722",
    ]
    for loc in located:
        assert text[loc.char_start : loc.char_end] == loc.element.value_raw


def test_duplicate_value_disambiguated_by_occurrence_order():
    # The same value printed twice: the k-th reported element gets the
    # k-th occurrence, so the two anchors are distinct and both real.
    text = "SSN 523-41-8722 was reissued; the prior card also showed 523-41-8722 there."
    first, second = locate_values(text, [_element("523-41-8722"), _element("523-41-8722")])
    assert first.char_start == text.index("523-41-8722")
    assert second.char_start == text.index("523-41-8722", first.char_end)
    assert first.char_start != second.char_start
    for loc in (first, second):
        assert text[loc.char_start : loc.char_end] == "523-41-8722"


def test_more_reports_than_occurrences_flags_the_excess():
    text = "SSN 523-41-8722 appears once."
    first, second = locate_values(text, [_element("523-41-8722"), _element("523-41-8722")])
    assert not first.offsets_missing
    assert second.offsets_missing
    assert second.char_start is None and second.char_end is None


def test_unmatched_value_flagged_without_disturbing_others():
    # A paraphrased/hallucinated value gets offsets_missing; neighbors
    # before AND after still anchor normally.
    text = "SSN 523-41-8722 and phone (415) 555-0137 on file."
    located = locate_values(
        text,
        [
            _element("523-41-8722"),
            _element("523418722"),  # model stripped the dashes -> not verbatim
            _element("(415) 555-0137", ElementType.PHONE),
        ],
    )
    assert [loc.offsets_missing for loc in located] == [False, True, False]
    assert text[located[2].char_start : located[2].char_end] == "(415) 555-0137"


def test_matching_is_byte_exact_no_case_folding():
    text = "Contact MARIA.ALVAREZ12@EXAMPLE.NET for details."
    (located,) = locate_values(text, [_element("maria.alvarez12@example.net", ElementType.EMAIL)])
    assert located.offsets_missing


def test_empty_elements_yield_empty_result():
    assert locate_values("any passage text", []) == []


def test_located_element_exposes_offsets_missing_property():
    present = LocatedElement(element=_element("x"), char_start=0, char_end=1)
    absent = LocatedElement(element=_element("x"), char_start=None, char_end=None)
    assert not present.offsets_missing
    assert absent.offsets_missing
