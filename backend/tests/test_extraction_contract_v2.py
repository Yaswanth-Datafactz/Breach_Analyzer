"""Tests for the v2 extraction contract (docs/plan.md D9): offsets and
mention-level dob removed from the model-facing schema, versions bumped
with v1 documented for spike attribution, staff-signature rule hardened
to full omission, cache-friendly stable-prefix ordering preserved."""

import json

import pytest
from pydantic import ValidationError

from app.services.extraction import prompts, schemas
from app.services.extraction.schemas import (
    ElementOut,
    ElementType,
    MentionOut,
    PassageExtraction,
)

# --- version stamps ---------------------------------------------------------


def test_versions_bumped_to_v2():
    assert schemas.SCHEMA_VERSION == "v2"
    assert prompts.PROMPT_VERSION == "v2"


def test_v1_documented_for_spike_attribution():
    # Spike rows are stamped "v1"; the module docstrings must keep saying
    # what v1 meant so those rows stay attributable (D9).
    assert "v1" in schemas.__doc__
    assert "char_start" in schemas.__doc__
    assert "v1" in prompts.__doc__


# --- schema shape (D9 removals) ---------------------------------------------


def test_element_out_has_no_model_reported_offsets():
    assert "char_start" not in ElementOut.model_fields
    assert "char_end" not in ElementOut.model_fields


def test_mention_out_has_no_dob():
    assert "dob" not in MentionOut.model_fields
    assert set(MentionOut.model_fields) == {"name_raw", "confidence"}


def test_dob_still_exists_as_element_type():
    # dob moved homes, it did not disappear: it arrives as an element.
    assert ElementType.DOB.value == "dob"
    element = ElementOut(
        element_type=ElementType.DOB, value_raw="1972-08-15", mention_ref=0, confidence=0.9
    )
    PassageExtraction(
        mentions=[MentionOut(name_raw="Maria Alvarez", confidence=1.0)], elements=[element]
    )


# --- round-trip -------------------------------------------------------------


def _valid_payload() -> dict:
    return {
        "mentions": [{"name_raw": "Maria Alvarez", "confidence": 1.0}],
        "elements": [
            {
                "element_type": "ssn",
                "value_raw": "523-41-8722",
                "mention_ref": 0,
                "confidence": 0.95,
            },
            {
                "element_type": "name",
                "value_raw": "Maria Alvarez",
                "mention_ref": 0,
                "confidence": 1.0,
            },
            {
                "element_type": "ssn",
                "value_raw": "601-88-4415",
                "mention_ref": None,
                "confidence": 0.4,
            },
        ],
    }


def test_valid_model_response_round_trips():
    parsed = PassageExtraction.model_validate(_valid_payload())
    assert parsed.schema_version == "v2"
    assert parsed.elements[0].element_type is ElementType.SSN
    assert parsed.elements[2].mention_ref is None  # orphan values stay legal
    reparsed = PassageExtraction.model_validate(json.loads(parsed.model_dump_json()))
    assert reparsed == parsed


def test_v1_style_offset_keys_are_ignored_not_fatal():
    # A model that habitually emits offsets anyway must not fail
    # validation over data the service discards regardless (schemas.py
    # docstring): the keys are dropped at the boundary.
    payload = _valid_payload()
    payload["elements"][0]["char_start"] = 51
    payload["elements"][0]["char_end"] = 62
    payload["mentions"][0]["dob"] = "1972-08-15"
    parsed = PassageExtraction.model_validate(payload)
    dumped = parsed.model_dump()
    assert "char_start" not in dumped["elements"][0]
    assert "dob" not in dumped["mentions"][0]


def test_dangling_mention_ref_still_fails_loudly():
    payload = _valid_payload()
    payload["elements"][0]["mention_ref"] = 3
    with pytest.raises(ValidationError, match="mention_ref"):
        PassageExtraction.model_validate(payload)


def test_empty_result_is_valid():
    parsed = PassageExtraction.model_validate({"mentions": [], "elements": []})
    assert parsed.mentions == [] and parsed.elements == []


# --- prompt contract --------------------------------------------------------


def test_prompt_no_longer_asks_for_offsets():
    assert "char_start" not in prompts.SYSTEM_PROMPT
    assert "char_end" not in prompts.SYSTEM_PROMPT
    # The one remaining mention of offsets is the explicit instruction NOT
    # to report them.
    assert "Do not report character positions or offsets" in prompts.SYSTEM_PROMPT


def test_prompt_field_names_derive_from_v2_models():
    # UC2's live-verified lesson: exact key names in the prompt text,
    # built from the models -- so the v2 removals propagate automatically.
    assert "name_raw, confidence" in prompts.SYSTEM_PROMPT
    assert "element_type, value_raw, mention_ref, confidence" in prompts.SYSTEM_PROMPT
    assert "- dob:" not in prompts.SYSTEM_PROMPT


def test_staff_signature_rule_is_full_omission():
    rules = prompts.SYSTEM_PROMPT
    assert "OMITTED ENTIRELY" in rules
    assert "not at any confidence" in rules
    # The v1 hedge the spike measured (staff included at 0.3) must have no
    # textual foothold: reduced confidence is named as the wrong behavior.
    assert "including a handler with a low confidence score is wrong" in rules.lower()


def test_trap_rules_survive_v2():
    for marker in ("Order #123-45-6789", "XXX-XX-1234", "{{ssn}}", "TEST, SAMPLE"):
        assert marker in prompts.SYSTEM_PROMPT, marker


def test_passage_comes_last_for_prefix_caching():
    system_a, user_a = prompts.build_tier1_prompt("PASSAGE-A-TEXT")
    system_b, user_b = prompts.build_tier1_prompt("PASSAGE-B-TEXT")
    assert system_a == system_b  # stable prefix, byte-identical across calls
    assert user_a.endswith("PASSAGE-A-TEXT</passage>")
    prefix_a = user_a[: user_a.index("PASSAGE-A-TEXT")]
    prefix_b = user_b[: user_b.index("PASSAGE-B-TEXT")]
    assert prefix_a == prefix_b  # user template identical up to the passage


def test_repair_prompt_carries_errors_and_passage():
    raw = {"mentions": [], "elements": [{"element_type": "ssn"}]}
    errors = [{"loc": "elements.0.value_raw", "msg": "Field required"}]
    system, user = prompts.build_repair_prompt(raw, errors, "THE PASSAGE TEXT")
    assert system == prompts.SYSTEM_PROMPT
    assert "elements.0.value_raw" in user
    assert "Field required" in user
    assert user.endswith("THE PASSAGE TEXT</passage>")
    assert json.dumps(raw, default=str) in user
