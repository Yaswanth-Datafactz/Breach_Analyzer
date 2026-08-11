"""Deterministic offset computation for LLM extractions (docs/plan.md D9).

The Day-1 spike measured model-reported char offsets 0/10 correct while
`passage.find(value_raw)` located every planted value uniquely -- so v2
removed offsets from the model-facing contract (schemas.py) and this
module computes them instead: exact string match of the verbatim
`value_raw` against the exact passage text the model was shown.

Disambiguation is by OCCURRENCE ORDER: when the model reports the same
value_raw N times (e.g. one printed value attached to two contexts, or a
value genuinely printed twice), the k-th reported element gets the k-th
non-overlapping occurrence in the passage. That is the only ordering the
model can express -- it has no other channel to say "the second one".

A value that does not exact-match anywhere (the model paraphrased,
reformatted, or hallucinated it) gets NO offsets and is flagged
`offsets_missing` -- the pipeline routes those to the review queue
(plan §4 `review_items`, kind=extraction) instead of guessing an anchor.
Matching is deliberately strict (byte-exact, no case folding, no
whitespace normalization): the spike measured 8/8 byte-exact recall, and
a fuzzy anchor is exactly the unverifiable provenance claim the
defensibility bar forbids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.logging import get_logger
from app.services.extraction.schemas import ElementOut

logger = get_logger("extraction.offsets")


@dataclass(frozen=True)
class LocatedElement:
    """One extracted element with its service-computed anchor. Offsets are
    into the passage text exactly as sent to the model; char_end is
    exclusive, so passage_text[char_start:char_end] == element.value_raw
    whenever offsets_missing is False."""

    element: ElementOut
    char_start: int | None
    char_end: int | None

    @property
    def offsets_missing(self) -> bool:
        return self.char_start is None


def locate_values(
    passage_text: str, elements: Sequence[ElementOut]
) -> list[LocatedElement]:
    """Anchor every element's value_raw in the passage by exact string
    match, occurrence order disambiguating duplicates. Preserves input
    order; every input element yields exactly one LocatedElement."""
    next_search_from: dict[str, int] = {}
    located: list[LocatedElement] = []
    for element in elements:
        value = element.value_raw
        start = passage_text.find(value, next_search_from.get(value, 0))
        if start == -1:
            # Never log the value itself (CLAUDE.md: value_raw is redacted
            # from logs; type + position metadata is enough to triage).
            logger.warning(
                "offsets_missing",
                element_type=element.element_type.value,
                value_length=len(value),
                elements_total=len(elements),
            )
            located.append(LocatedElement(element=element, char_start=None, char_end=None))
            continue
        next_search_from[value] = start + len(value)
        located.append(
            LocatedElement(element=element, char_start=start, char_end=start + len(value))
        )
    return located
