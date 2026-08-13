"""The provider-agnostic extraction adapter contract. Copied near-verbatim
from Document_Extraction/backend/app/services/extraction/base.py (UC2). All
concrete adapters return the SAME `ExtractionResult` shape -- a raw
(unvalidated) dict plus usage/logprobs -- so validation has exactly one path
regardless of which provider produced it (tier 1 DeepSeek here now; tier 2
OpenAI lands in extraction/openai_adapter.py with its service, phase B2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractionUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class ExtractionResult:
    raw_json: dict
    usage: ExtractionUsage
    # Per-token logprobs, provider-native shape, kept opaque here -- the
    # confidence composite interprets these. DeepSeek populates this (its
    # API exposes logprobs); the OpenAI tier-2 adapter's models are
    # reasoning models that reject the logprobs parameter outright (see
    # extraction/openai_adapter.py's module docstring), so that adapter
    # leaves this None and the composite renormalizes -- the exact
    # signal-loss UC2 documented for its tier 2, accepted with eyes open
    # again (docs/plan.md D3), now for a provider-specific reason instead
    # of an API-wide one.
    logprobs: list[dict] | None = None


class ExtractionAdapter(ABC):
    provider_id: str
    supports_vision: bool = False

    @abstractmethod
    async def extract_text(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        """Extract from passage text alone."""

    async def extract_image(
        self, *, system_prompt: str, user_prompt: str, image_png_b64: str, schema_json: dict
    ) -> ExtractionResult:
        raise NotImplementedError(f"{self.provider_id} does not support vision input (docs/plan.md D3)")

    @abstractmethod
    async def repair(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        """One bounded repair call (docs/plan.md §3: Pydantic-validated JSON,
        one bounded repair): given only the fields that failed validation and
        their error messages (baked into user_prompt by the caller), return a
        corrected raw dict. Never called more than once per extraction."""

    @abstractmethod
    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> ExtractionResult:
        """Free-form JSON completion, NOT bound to the extraction schema --
        for calls whose response shape the strict schema-bound methods can't
        produce (UC2 used this for its decoupled critic)."""
