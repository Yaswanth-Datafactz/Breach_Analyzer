"""Unit tests for the structlog redaction processor (CLAUDE.md data-safety
hard rule: never log pii_elements.value_raw). Tests the processor as a pure
function -- exactly how structlog will call it -- rather than capturing
rendered output, which would test the JSONRenderer too."""

from app.core.logging import _REDACTED_KEYS, redact_sensitive


def test_top_level_sensitive_keys_are_redacted():
    event = {
        "event": "element_detected",
        "value_raw": "123-45-6789",
        "value": "123456789",
        "password": "hunter2",
        "api_key": "sk-not-a-real-key",
        "element_type": "ssn",
    }
    out = redact_sensitive(None, "info", event)
    for key in ("value_raw", "value", "password", "api_key"):
        assert out[key] == "[REDACTED]"
    # Non-sensitive keys pass through untouched.
    assert out["element_type"] == "ssn"
    assert out["event"] == "element_detected"


def test_nested_dicts_and_lists_are_redacted():
    # The realistic leak path: a pii_element serialized whole into a log
    # kwarg, not a top-level `value_raw=`.
    event = {
        "event": "batch_persisted",
        "elements": [
            {"element_type": "ssn", "value_raw": "123-45-6789", "confidence": 0.9},
            {"element_type": "email", "value_raw": "bob@example.com", "confidence": 0.8},
        ],
        "detail": {"credentials": {"password": "hunter2"}},
    }
    out = redact_sensitive(None, "info", event)
    assert all(e["value_raw"] == "[REDACTED]" for e in out["elements"])
    assert out["detail"]["credentials"]["password"] == "[REDACTED]"
    # Siblings survive.
    assert out["elements"][0]["element_type"] == "ssn"
    assert out["elements"][0]["confidence"] == 0.9


def test_non_sensitive_events_pass_through_unchanged():
    event = {"event": "reaped_stale_jobs", "count": 0, "stub": True}
    out = redact_sensitive(None, "info", event)
    assert out == {"event": "reaped_stale_jobs", "count": 0, "stub": True}


def test_redacted_key_set_covers_the_hard_rule():
    # The CLAUDE.md rule names value_raw explicitly; the rest are the
    # credential keys docs/plan.md §11 forbids from logs.
    assert {"value_raw", "value", "password", "api_key"} <= set(_REDACTED_KEYS)
