"""Provider-agnostic extraction adapters (docs/plan.md D3) -- one ABC,
settings-selected concrete implementations, mirroring UC2's adapter
pattern. Tier 2 (Anthropic, text + vision) lands in extraction/claude.py
with its service in phase B2 -- get_tier2_adapter appears then, never
before it genuinely works.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.extraction.base import ExtractionAdapter
from app.services.extraction.deepseek import DeepSeekExtractionAdapter


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


__all__ = ["ExtractionAdapter", "DeepSeekExtractionAdapter", "get_tier1_adapter"]
