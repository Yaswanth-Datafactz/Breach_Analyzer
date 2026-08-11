"""The tier-1/tier-2 extraction output schema (docs/plan.md §3/§4) -- what
every LLM extraction call must return, regardless of provider. Follows the
shape of UC2's invoice_extraction.py where the reasoning carries over:

- `SCHEMA_VERSION` is stamped on every `extraction_jobs` row so an accuracy
  number is always attributable to the exact schema that produced it (UC2
  convention).
- Values stay verbatim strings at the model-output boundary (`value_raw`,
  `dob` as printed) -- services/er/normalize.py owns parsing/normalizing,
  the single shared path validation, ER, and the accuracy scorer all call
  (UC2's DateField reasoning).
- Unlike UC2, `confidence` IS a model-reported field here -- but it is one
  minor input to the stored composite (services/confidence.py weights
  logprob-derived signals above it), never the persisted confidence by
  itself. UC2 documented self-reported confidence's "always 0.95" failure
  mode; keeping the field lets tier-0/tier-1 disagreement checks see what
  the model *claims* while the composite stays independent of the claim.
- `char_start`/`char_end` are offsets into the exact passage text the model
  was shown -- the evidence anchor `pii_elements` and `flag_evidence`
  resolve to (docs/plan.md §1: every flag must trace to a passage). They are
  structurally validated here (ordered, in-bounds refs); whether the model's
  offsets actually land on `value_raw` is verified independently downstream
  (UC2's grounding lesson: never trust a provenance claim unchecked).

Validation strictness is deliberate: a violation triggers the one bounded
repair (docs/plan.md §3), and after that the document escalates -- these
models never silently coerce a malformed extraction into a plausible one.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "v1"


class ElementType(str, Enum):
    """The §4 `pii_elements.element_type` enum, verbatim. One flag category
    per member (plus `name`, which anchors mentions rather than flying
    solo) -- the DB CHECK constraint in db/models.py lists the same set, so
    a value that validates here always inserts cleanly."""

    SSN = "ssn"
    SSN_LAST4 = "ssn_last4"
    DOB = "dob"
    DRIVERS_LICENSE = "drivers_license"
    PASSPORT = "passport"
    FINANCIAL_ACCOUNT = "financial_account"
    CREDIT_CARD = "credit_card"
    MEDICAL = "medical"
    CREDENTIAL = "credential"
    ADDRESS = "address"
    PHONE = "phone"
    EMAIL = "email"
    NAME = "name"


class MentionOut(BaseModel):
    """One person mention in the passage -- the unit ER clusters (§4
    `mentions`). `name_raw` is the name exactly as printed (nickname,
    initials, "Last, First" -- variant detection is ER's job, not the
    model's). `dob` rides along when the passage states it for this person,
    verbatim string, parsed later."""

    name_raw: str = Field(min_length=1)
    dob: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class ElementOut(BaseModel):
    """One personal data element found in the passage (§4 `pii_elements`).
    `mention_ref` is a 0-based index into the sibling `mentions` list --
    null is legal and load-bearing: the PartialIdentifiers scenario plants
    SSNs with no name in sight, and forcing an attribution there would
    manufacture exactly the false links ER exists to prevent."""

    element_type: ElementType
    value_raw: str = Field(min_length=1)
    mention_ref: int | None = Field(default=None, ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _offsets_ordered(self) -> ElementOut:
        # char_end is exclusive, so an empty span can never be a valid
        # evidence anchor.
        if self.char_end <= self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) must be greater than char_start ({self.char_start})"
            )
        return self


class PassageExtraction(BaseModel):
    """The full output for one passage: every person mention plus every
    element, elements attached to mentions by index. This is the ONE shape
    both tier adapters' raw JSON is validated against (base.py: validation
    has exactly one path regardless of provider)."""

    schema_version: str = SCHEMA_VERSION
    mentions: list[MentionOut] = Field(default_factory=list)
    elements: list[ElementOut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mention_refs_resolve(self) -> PassageExtraction:
        # A dangling mention_ref would silently attach an element to nobody
        # (or worse, to whoever lands at that index after a repair) -- fail
        # loudly here so the bounded repair sees it as a named error.
        for i, element in enumerate(self.elements):
            if element.mention_ref is not None and element.mention_ref >= len(self.mentions):
                raise ValueError(
                    f"elements[{i}].mention_ref ({element.mention_ref}) does not index into "
                    f"mentions (length {len(self.mentions)})"
                )
        return self
