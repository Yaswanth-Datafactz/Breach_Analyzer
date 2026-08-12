"""Anthropic (Claude) extraction adapter -- the tier-2 escalation path,
text + vision (docs/plan.md D3: `claude-sonnet-4-6`). Structure mirrors
deepseek.py: same ExtractionAdapter ABC, same "return raw dict + usage,
validation happens in exactly one place" contract (base.py).

Differences from the DeepSeek adapter, each deliberate:

- Official `anthropic` SDK (AsyncAnthropic), not hand-rolled httpx -- the
  SDK owns retries/timeouts and is the same surface the agent layer builds
  on (one provider, one client library).
- Prompt caching (docs/plan.md §9: "Anthropic prompt caching on the stable
  system-prompt+schema prefix"): the system prompt -- byte-identical across
  every extraction call by prompts.py construction -- is sent as a system
  content block with `cache_control: {"type": "ephemeral"}`. The volatile
  passage rides in the user message AFTER that prefix, so repeat calls
  read the instructions+schema from cache at ~0.1x input price. Cache
  effectiveness is measured from usage, never assumed (D9's rule about
  unverified cache savings).
- Usage: Anthropic's `usage.input_tokens` EXCLUDES cached tokens, unlike
  DeepSeek's `prompt_tokens` which includes them. This adapter normalizes
  to the ExtractionUsage convention (input_tokens = total prompt tokens,
  cached_input_tokens = the subset served from cache) so cost math in
  extraction/costing.py has exactly one shape to price. Cache WRITES
  (cache_creation_input_tokens, billed 1.25x) are folded into the uncached
  input count -- a deliberate ~25%-of-first-write underestimate, flagged
  here so the Friday cost report can call it out rather than discover it.
- No logprobs: the Anthropic API does not expose them, so
  ExtractionResult.logprobs stays None and the confidence composite
  renormalizes around the missing signal (services/confidence.py) -- the
  exact signal loss UC2 documented for its tier 2, accepted with eyes open
  (D3).
- Vision (supports_vision = True): extract_image sends the page PNG as a
  base64 image block ahead of the text prompt -- the §14b R1 path that
  carries the BulkSpreadsheet PNGs tier-0/tier-1 cannot read.
- No sampling parameters: newer Anthropic models reject temperature/top_p
  outright, and determinism here is carried by the prompt contract -- so
  the request stays valid regardless of which model config points at.

NO live call happens anywhere in tests: tests inject a fake client object
exposing `messages.create` (the SDK client's exact surface), same pattern
as deepseek.py's MockTransport injection.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from app.services.extraction.base import ExtractionAdapter, ExtractionResult, ExtractionUsage

# Plenty for a PassageExtraction JSON over a ~3K-token chunk; far below the
# streaming-required threshold, so plain awaited create() is safe.
_MAX_TOKENS = 4096

_REQUEST_TIMEOUT_SECONDS = 120.0


class ClaudeExtractionAdapter(ExtractionAdapter):
    provider_id = "anthropic"
    supports_vision = True

    def __init__(
        self,
        api_key: str,
        model: str,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = _MAX_TOKENS,
    ):
        self._model = model
        self._max_tokens = max_tokens
        # Tests inject a fake client exposing `messages.create` -- request/
        # response shape is verified without ever making a live call.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def extract_text(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(system_prompt, [{"type": "text", "text": user_prompt}])
        return self._parse_response(response)

    async def extract_image(
        self, *, system_prompt: str, user_prompt: str, image_png_b64: str, schema_json: dict
    ) -> ExtractionResult:
        # Image first, instructions after -- the standard Anthropic vision
        # ordering; the cached system prefix is unaffected either way.
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image_png_b64},
            },
            {"type": "text", "text": user_prompt},
        ]
        response = await self._call(system_prompt, content)
        return self._parse_response(response)

    async def repair(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(system_prompt, [{"type": "text", "text": user_prompt}])
        return self._parse_response(response)

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> ExtractionResult:
        response = await self._call(system_prompt, [{"type": "text", "text": user_prompt}])
        return self._parse_response(response)

    async def _call(self, system_prompt: str, content: list[dict]) -> Any:
        return await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": content}],
        )

    def _parse_response(self, response: Any) -> ExtractionResult:
        text = next(
            (block.text for block in response.content if getattr(block, "type", None) == "text"),
            None,
        )
        if text is None:
            # A refusal / empty response has no text block -- surface it as
            # the same failure class as malformed JSON so the caller's
            # bounded-repair/escalation path owns it (never silently coerced).
            raise ValueError(
                f"anthropic response contained no text block (stop_reason="
                f"{getattr(response, 'stop_reason', None)!r})"
            )
        raw_json = json.loads(_strip_code_fence(text))
        usage = response.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return ExtractionResult(
            raw_json=raw_json,
            usage=ExtractionUsage(
                # Normalize to "input_tokens includes cached" (module docstring).
                input_tokens=usage.input_tokens + cached + cache_creation,
                output_tokens=usage.output_tokens,
                cached_input_tokens=cached,
            ),
            logprobs=None,  # not exposed by the API (module docstring)
        )


def _strip_code_fence(text: str) -> str:
    """The prompt forbids code fences, but a fenced response is trivially
    recoverable JSON -- stripping it here beats burning the one bounded
    repair call on formatting."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped
