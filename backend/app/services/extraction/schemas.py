"""The tier-1/tier-2 extraction output schema (docs/plan.md §3/§4) -- what
every LLM extraction call must return, regardless of provider. Follows the
shape of UC2's invoice_extraction.py where the reasoning carries over:

- `SCHEMA_VERSION` is stamped on every `extraction_jobs` row so an accuracy
  number is always attributable to the exact schema that produced it (UC2
  convention). **v1** (the Day-1 DeepSeek spike contract, Aug 11) carried
  `char_start`/`char_end` on elements and `dob` on mentions -- spike rows
  stamped "v1" mean exactly that shape.
- **v2** (this file) applies the D9 spike findings:
  * `char_start`/`char_end` REMOVED from the model-facing contract. The
    spike measured model-reported offsets 0/10 correct (start positions
    drift progressively -- the model cannot count characters) while
    `passage.find(value_raw)` located every planted value uniquely. The
    extraction service now computes offsets deterministically
    (extraction/offsets.py); `pii_elements` keeps its offset columns --
    only who fills them changed.
  * `MentionOut.dob` REMOVED. Redundant with `dob` elements
    (element_type="dob" + mention_ref); with two homes the spike model
    filled only one, so the schema now has exactly one.
- Values stay verbatim strings at the model-output boundary (`value_raw`
  as printed) -- services/er/normalize.py owns parsing/normalizing,
  the single shared path validation, ER, and the accuracy scorer all call
  (UC2's DateField reasoning).
- `confidence` IS still a model-reported field -- but the spike confirmed
  it is bimodal (1.0/0.3) and uninformative, so the stored composite
  (services/confidence.py) leans on logprob-derived signals, which the
  spike confirmed survive the Foundry passthrough (D9). Keeping the field
  lets tier-0/tier-1 disagreement checks see what the model *claims*
  while the composite stays independent of the claim.

Validation strictness is deliberate: a violation triggers the one bounded
repair (docs/plan.md §3), and after that the document escalates -- these
models never silently coerce a malformed extraction into a plausible one.
Unknown keys are ignored (Pydantic default), so a model that habitually
emits v1-style offset fields anyway loses them at the boundary instead of
failing validation over data we would discard regardless.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "v2"


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
    model's). A date of birth is NOT carried here (v2): it arrives as an
    element (element_type="dob") whose mention_ref points back at this
    mention -- one home per fact."""

    name_raw: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ElementOut(BaseModel):
    """One personal data element found in the passage (§4 `pii_elements`).
    `mention_ref` is a 0-based index into the sibling `mentions` list --
    null is legal and load-bearing: the PartialIdentifiers scenario plants
    SSNs with no name in sight, and forcing an attribution there would
    manufacture exactly the false links ER exists to prevent.

    No offsets here (v2/D9): the service string-matches `value_raw` back
    into the passage (extraction/offsets.py), which is why value_raw being
    VERBATIM is a hard requirement, not a preference."""

    element_type: ElementType
    value_raw: str = Field(min_length=1)
    mention_ref: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0.0, le=1.0)


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
