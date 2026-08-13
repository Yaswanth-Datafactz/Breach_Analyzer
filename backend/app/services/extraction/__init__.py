"""Provider-agnostic extraction adapters (docs/plan.md D3) -- one ABC,
settings-selected concrete implementations, mirroring UC2's adapter
pattern. Tier 1 is DeepSeek (text only); tier 2 is OpenAI
(text + vision, extraction/openai_adapter.py) and is OPTIONAL at runtime:
without an OpenAI key there is no tier-2 adapter and the extraction service
degrades gracefully (review flags instead of escalations) -- the same
config-only contract the pipeline applies to tier 1.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.extraction.base import ExtractionAdapter
from app.services.extraction.deepseek import DeepSeekExtractionAdapter
from app.services.extraction.openai_adapter import OpenAIExtractionAdapter


def get_tier1_adapter(settings: Settings) -> ExtractionAdapter:
    """Tier 1: cheap text-only extraction (docs/plan.md D3). DeepSeek has
    no vision support, so this is only ever called on parsed/OCR'd text,
    never on a page image."""
    return DeepSeekExtractionAdapter(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        api_version=settings.deepseek_api_version,
        model=settings.deepseek_model,
    )


def get_tier2_adapter(settings: Settings) -> ExtractionAdapter | None:
    """Tier 2: OpenAI text+vision escalation (docs/plan.md D3). Returns
    None when no key is configured -- callers treat that as 'escalation
    unavailable', never as an error (the keyed run later is config-only)."""
    if not settings.openai_api_key:
        return None
    return OpenAIExtractionAdapter(
        api_key=settings.openai_api_key,
        model=settings.tier2_model,
    )


__all__ = [
    "ExtractionAdapter",
    "DeepSeekExtractionAdapter",
    "OpenAIExtractionAdapter",
    "get_tier1_adapter",
    "get_tier2_adapter",
]
