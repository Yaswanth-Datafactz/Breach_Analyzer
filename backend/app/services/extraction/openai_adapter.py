"""OpenAI extraction adapter -- the tier-2 escalation path, text + vision
(docs/plan.md D3, revised 2026-08-12: Anthropic swapped out for OpenAI --
no Anthropic key is available in this environment, only an OpenAI one; see
core/config.py's OpenAI settings block for the model choice + a pricing
citation. REVISED AGAIN 2026-08-13: that key is an Azure AI Foundry
resource key, confirmed live -- this adapter's client is AsyncAzureOpenAI,
not AsyncOpenAI, see __init__ and config.py's OpenAI settings block for the
full endpoint-discovery story). Structure mirrors deepseek.py: same
ExtractionAdapter ABC, same "return raw dict + usage, validation happens in
exactly one place" contract (base.py).

Named openai_adapter.py, not openai.py -- a module in this package sharing
the `openai` SDK's own name would risk shadowing `import openai` below
(the exact footgun the task that produced this file called out explicitly).

Differences from the DeepSeek adapter and from the retired Anthropic
adapter, each deliberate and each checked against OpenAI's own docs/
community reports on 2026-08-12 rather than assumed:

- Official `openai` SDK (AsyncOpenAI), not hand-rolled httpx -- same
  reasoning the retired Anthropic adapter gave: the SDK owns retries/
  timeouts and is the client surface the agent layer builds on too
  (model_client.py).
- `chat.completions.create`, not the newer Responses API. OpenAI's own
  reasoning-models guide (developers.openai.com/api/docs/guides/reasoning)
  says reasoning models "work better with the Responses API" -- Chat
  Completions "is still supported" but is a deliberate choice here, not an
  oversight: it is the more direct match for this codebase's existing
  shape (one stateless call, full message history resent every turn,
  exactly like the Anthropic Messages API this replaces and DeepSeek's own
  OpenAI-compatible endpoint), with no server-side conversation state to
  reconcile against the DB-persisted, replayable history this system
  already keeps. It also sidesteps a real, documented Responses-API-only
  gap: community reports (a GPT-5.1/5.2 bug thread) show
  `message.output_text.logprobs` coming back EMPTY on the Responses API
  when Structured Outputs is enabled, whereas Chat Completions has no such
  reported gap (the `structured-logprobs` library combines `logprobs=True`
  with `response_format=json_schema` successfully via Chat Completions,
  confirmed against gpt-4o). Moot for THIS adapter regardless (next
  point), but it is why Chat Completions remains the right base even if a
  future model in this family restores logprobs.
- No logprobs, no temperature/top_p: gpt-5.5 is a reasoning model, and
  OpenAI's reasoning-models guide plus multiple 2026 community
  reports document `logprobs` / `top_logprobs` / `temperature` / `top_p` /
  the penalty params as UNSUPPORTED on reasoning models via Chat
  Completions -- sending them 400s. This adapter never sends them.
  ExtractionResult.logprobs stays None and the confidence composite
  renormalizes around the missing signal (services/confidence.py) -- the
  same signal-loss SHAPE the retired Anthropic adapter already had, for a
  different root cause (there: the API had no logprobs concept at all;
  here: the API has one but this model class rejects the parameter) --
  both honestly documented rather than silently assumed away.
- `max_completion_tokens`, not `max_tokens`: reasoning models reject the
  legacy field name (output + internal reasoning share one budget under
  the new name).
- Structured outputs -- a genuine capability the Anthropic Messages API
  used here had no equivalent for (it relied on prompt text alone, same as
  DeepSeek still does): extract_text/extract_image/repair pass the
  caller-supplied `schema_json` (== PassageExtraction.model_json_schema(),
  computed once in extraction/service.py -- never hand-retyped here)
  through as `response_format: {"type": "json_schema", ...}`. `strict` is
  deliberately False: strict mode requires transforming the Pydantic-
  derived schema into OpenAI's narrower compliant subset
  (`additionalProperties: false` + every property listed as `required` +
  a restricted per-type keyword allowlist, per OpenAI's structured-outputs
  guide) -- a transform this keyless environment has no way to verify
  against the real API, and getting it subtly wrong under `strict: True`
  fails CLOSED (a hard 400) rather than open. Non-strict mode still
  meaningfully biases generation toward the schema -- strictly better than
  DeepSeek's bare `json_object` mode -- while the existing Pydantic-
  validate-plus-one-bounded-repair path (base.py, extraction/service.py)
  remains the actual correctness guarantee regardless of provider.
  Upgrading to `strict: True` with a properly verified schema transform is
  a good live-spike follow-up, not attempted here (VERIFY BEFORE FRIDAY --
  the same discipline docs/plan.md D3/D9 already apply to prices and
  offsets). `complete_json` (free-form, NOT schema-bound per base.py's ABC
  docstring) uses plain `{"type": "json_object"}` instead, matching its
  contract -- and is never passed a schema to begin with.
- Prompt caching is automatic and server-side: OpenAI caches repeated
  prompt prefixes (~1024+ tokens) with no code-level configuration --
  there is no equivalent of Anthropic's manual `cache_control` ephemeral-
  block plumbing to carry over, so it is simply gone, not replaced. The
  cached portion of a request is billed at the `cached_input` rate
  (MODEL_PRICES_USD_PER_MTOK) and reported in
  `usage.prompt_tokens_details.cached_tokens` -- the same field name
  deepseek.py already defensively checks as a Foundry-passthrough
  possibility, which corroborates it as the live OpenAI-compatible
  convention rather than a guess. Ordering still matters for the hit RATE
  even with no code-level switch: prompts.py's existing convention
  (stable system+schema prefix first, volatile passage last) is exactly
  what gives the automatic cache a stable prefix to key on. Effectiveness
  is UNMEASURED -- no live call has happened yet -- same D9-style honesty
  the rest of this codebase practices: never bank a caching saving before
  it is measured.
- Usage normalization is SIMPLER than the Anthropic adapter needed:
  OpenAI's `usage.prompt_tokens` already INCLUDES cached tokens (the same
  convention DeepSeek's Foundry passthrough uses), so there is no separate
  "cache write" figure to fold in the way Anthropic's
  `cache_creation_input_tokens` needed folding -- that adapter's
  documented ~25%-of-first-write underestimate simply does not apply here;
  OpenAI does not bill cache writes differently from normal input tokens.
- Vision (supports_vision = True): extract_image sends the page PNG as an
  `image_url` content part (`data:image/png;base64,...`) ahead of the text
  prompt -- OpenAI's standard vision content shape, carrying the same
  §14b R1 BulkSpreadsheet-PNG path tier-0/tier-1 cannot read.
- The system role is sent as `role: "system"`. OpenAI's docs name
  `"developer"` as the newer preferred role name for reasoning models but
  every reasoning-model generation so far (o1 onward) has kept accepting
  `"system"` as a backward-compatible alias; CONFIRMED live against this
  gpt-5.5 deployment 2026-08-13 (every probe call used `role: "system"`
  and succeeded) -- no longer a standing assumption.

NO live call happens anywhere in tests: tests inject a fake client object
exposing `chat.completions.create` (the SDK client's exact surface), same
pattern as deepseek.py's MockTransport injection and the retired Anthropic
adapter's fake `messages.create`.
"""

from __future__ import annotations

import json
from typing import Any

import openai

from app.services.extraction.base import ExtractionAdapter, ExtractionResult, ExtractionUsage

# Reasoning-model output headroom (module docstring: max_completion_tokens,
# not max_tokens). Generous enough for a PassageExtraction JSON over a
# ~3K-token chunk plus this model class's internal reasoning tokens, which
# share the same budget -- too tight a cap here truncates the JSON, not
# just the reasoning.
_MAX_COMPLETION_TOKENS = 8192

_REQUEST_TIMEOUT_SECONDS = 120.0


class OpenAIExtractionAdapter(ExtractionAdapter):
    provider_id = "openai"
    supports_vision = True

    def __init__(
        self,
        api_key: str,
        base_url: str,
        api_version: str,
        model: str,
        client: openai.AsyncOpenAI | None = None,
        max_completion_tokens: int = _MAX_COMPLETION_TOKENS,
    ):
        self._model = model
        self._max_completion_tokens = max_completion_tokens
        # Tests inject a fake client exposing `chat.completions.create` --
        # request/response shape is verified without ever making a live call.
        # Real construction (2026-08-13 revision) is AsyncAzureOpenAI, not
        # AsyncOpenAI -- this adapter's key is an Azure AI Foundry resource
        # key, confirmed live (see model_client.py + config.py's OpenAI
        # settings block for the full endpoint-discovery story; this
        # extraction adapter and the agent runner's model client hit the
        # same Foundry resource, same deployment).
        self._client = client or openai.AsyncAzureOpenAI(
            api_key=api_key, azure_endpoint=base_url, api_version=api_version, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def extract_text(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(
            system_prompt, [{"type": "text", "text": user_prompt}], schema_json=schema_json
        )
        return self._parse_response(response)

    async def extract_image(
        self, *, system_prompt: str, user_prompt: str, image_png_b64: str, schema_json: dict
    ) -> ExtractionResult:
        # Image first, instructions after -- mirrors the retired Anthropic
        # adapter's ordering; the cached (now automatic) system prefix is
        # unaffected either way.
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_png_b64}"}},
            {"type": "text", "text": user_prompt},
        ]
        response = await self._call(system_prompt, content, schema_json=schema_json)
        return self._parse_response(response)

    async def repair(self, *, system_prompt: str, user_prompt: str, schema_json: dict) -> ExtractionResult:
        response = await self._call(
            system_prompt, [{"type": "text", "text": user_prompt}], schema_json=schema_json
        )
        return self._parse_response(response)

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> ExtractionResult:
        response = await self._call(
            system_prompt, [{"type": "text", "text": user_prompt}], schema_json=None
        )
        return self._parse_response(response)

    async def _call(
        self, system_prompt: str, content: list[dict], *, schema_json: dict | None
    ) -> Any:
        if schema_json is not None:
            response_format: dict = {
                "type": "json_schema",
                "json_schema": {
                    "name": "passage_extraction",
                    "schema": schema_json,
                    # Deliberately non-strict -- module docstring explains why.
                    "strict": False,
                },
            }
        else:
            # complete_json is free-form, not schema-bound (base.py's ABC
            # docstring) -- plain JSON mode, same contract as DeepSeek's.
            response_format = {"type": "json_object"}
        return await self._client.chat.completions.create(
            model=self._model,
            max_completion_tokens=self._max_completion_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            response_format=response_format,
            # No temperature/top_p/logprobs -- module docstring: reasoning
            # models reject them outright.
        )

    def _parse_response(self, response: Any) -> ExtractionResult:
        choice = response.choices[0]
        text = choice.message.content
        if not text:
            # A refusal / empty response has no content -- surface it as the
            # same failure class as malformed JSON so the caller's bounded-
            # repair/escalation path owns it (never silently coerced).
            raise ValueError(
                f"openai response contained no message content (finish_reason="
                f"{getattr(choice, 'finish_reason', None)!r})"
            )
        raw_json = json.loads(_strip_code_fence(text))
        usage = response.usage
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", None) or 0
        return ExtractionResult(
            raw_json=raw_json,
            usage=ExtractionUsage(
                # OpenAI's prompt_tokens already INCLUDES cached tokens
                # (module docstring) -- no folding needed, unlike Anthropic.
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_input_tokens=cached,
            ),
            logprobs=None,  # reasoning models reject the param (module docstring)
        )


def _strip_code_fence(text: str) -> str:
    """Structured/JSON-object modes should never fence their own JSON, but
    a fenced response is trivially recoverable -- stripping it here beats
    burning the one bounded repair call on formatting."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped
