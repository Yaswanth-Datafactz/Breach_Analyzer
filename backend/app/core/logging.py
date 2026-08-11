"""Structured JSON logging via structlog (Handbook §6.2: structured logging).

Base copied verbatim from Document_Extraction/backend/app/core/logging.py
(UC2). One UC3-specific addition: the `redact_sensitive` processor, which
scrubs PII-bearing keys from every log event before rendering -- CLAUDE.md's
data-safety hard rule ("Never log pii_elements.value_raw") enforced in the
pipeline itself, not by reviewer discipline.
"""

import logging
import sys
from typing import Any

import structlog

# Keys whose values must never reach a log sink, at any nesting depth.
# `value_raw`/`value` cover pii_elements payloads; `password`/`api_key`
# cover credentials (docs/plan.md §11).
_REDACTED_KEYS = frozenset({"value_raw", "value", "password", "api_key"})

_REDACTED = "[REDACTED]"


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            key: _REDACTED if key in _REDACTED_KEYS else _redact(val) for key, val in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(item) for item in obj]
    return obj


def redact_sensitive(
    logger: object, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """structlog processor: replace sensitive values (recursively -- a
    pii_element serialized as a nested dict is the realistic leak path, not
    a top-level `value_raw=` kwarg) before the event is rendered."""
    for key in list(event_dict.keys()):
        if key in _REDACTED_KEYS:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


def configure_logging(log_level: str = "INFO") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "app") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
