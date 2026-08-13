"""OpenAIExtractionAdapter contract tests with a FAKE client object (the
SDK client's `chat.completions.create` surface) -- request shape and usage
normalization are verified without any live call, mirroring deepseek.py's
MockTransport pattern and the retired Claude adapter's fake `messages.create`
this file replaces (docs/plan.md D3, revised 2026-08-12: Anthropic swapped
out for OpenAI).

Renamed from test_extraction_claude_adapter.py; fixtures rebuilt around
AsyncOpenAI's `chat.completions.create` shape instead of AsyncAnthropic's
`messages.create` shape. Mapping from the retired suite, one test at a time:

- cache_control (Anthropic's manual ephemeral cache-breakpoint plumbing) has
  no OpenAI equivalent -- prompt caching there is automatic and server-side
  (openai_adapter.py's module docstring), so there is nothing left in the
  request to assert on. Repurposed into the assertion that IS load-bearing
  for this provider instead: gpt-5.6-sol/terra are reasoning models that
  400 on temperature/top_p/logprobs, so every call must omit them and use
  `max_completion_tokens`, never the legacy `max_tokens`.
- usage normalization incl. cached tokens -- kept, but the arithmetic
  flips: Anthropic reported input tokens EXCLUDING cache reads/writes (the
  adapter had to fold `cache_read_input_tokens` + `cache_creation_input_tokens`
  into the total); OpenAI's `usage.prompt_tokens` already INCLUDES cached
  tokens, so `result.usage.input_tokens` is just `usage.prompt_tokens`
  unchanged, with `cached_tokens` reported alongside from a separate field.
- vision content-block shape -- kept, block type/key names updated
  (`image` + `source.data` -> `image_url` + `image_url.url` data-URI).
- JSON parse / fence-recovery -- kept as-is (`_strip_code_fence` is
  provider-agnostic).
- refusal / no-content handling -- kept, updated to OpenAI's shape (empty
  `choice.message.content`, not Anthropic's empty `content` block list).
- provider_id / supports_vision -- kept; provider_id is now "openai".
- repair/complete_json sharing the call shape -- kept, re-pointed at the
  real difference between the two for THIS provider: repair is still
  schema-bound (`response_format: json_schema`), complete_json is free-form
  (`response_format: json_object`), per base.py's ABC contract.

New (no analogue in the retired suite, added because this adapter's own
logic needs it exercised): the schema_json wiring into a non-strict
`response_format.json_schema` block, and the defensive cached-token-details
fallback to 0 when `prompt_tokens_details` is absent from usage.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.extraction.openai_adapter import _MAX_COMPLETION_TOKENS, OpenAIExtractionAdapter

_PAYLOAD = {"mentions": [], "elements": []}
_SCHEMA = {"type": "object", "properties": {"mentions": {"type": "array"}}}


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeClient:
    def __init__(self, response):
        self.chat = _FakeChat(response)


def _response(text, *, prompt_tokens=130, completion_tokens=50, cached_tokens=30, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason),
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        ),
    )


def _adapter(response):
    client = _FakeClient(response)
    return OpenAIExtractionAdapter(api_key="unused", model="gpt-5.6-terra", client=client), client


def test_extract_text_sends_json_schema_response_format_and_normalizes_usage():
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    result = asyncio.run(
        adapter.extract_text(system_prompt="SYSTEM", user_prompt="USER", schema_json=_SCHEMA)
    )

    (request,) = client.chat.completions.requests
    assert request["model"] == "gpt-5.6-terra"
    assert request["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": [{"type": "text", "text": "USER"}]},
    ]
    # non-strict json_schema mode -- module docstring explains why strict
    # is deliberately False (Pydantic-derived schemas aren't pre-transformed
    # into OpenAI's stricter compliant subset).
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "passage_extraction", "schema": _SCHEMA, "strict": False},
    }

    assert result.raw_json == _PAYLOAD
    # OpenAI's prompt_tokens ALREADY includes cached tokens -- no folding
    # needed, unlike the retired Anthropic adapter.
    assert result.usage.input_tokens == 130
    assert result.usage.cached_input_tokens == 30
    assert result.usage.output_tokens == 50
    assert result.logprobs is None  # reasoning models reject the logprobs param


def test_reasoning_model_call_omits_sampling_params_and_uses_max_completion_tokens():
    """The load-bearing request-shape assertion for this provider, replacing
    the retired suite's cache_control test (module docstring: OpenAI prompt
    caching is automatic/server-side -- nothing left to assert in the
    request). gpt-5.6-sol/terra are reasoning models: temperature/top_p/
    logprobs/top_logprobs/penalty params 400 on Chat Completions, and the
    output-budget field is renamed max_completion_tokens."""
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    asyncio.run(adapter.extract_text(system_prompt="S", user_prompt="U", schema_json=_SCHEMA))

    (request,) = client.chat.completions.requests
    forbidden = (
        "temperature", "top_p", "logprobs", "top_logprobs",
        "presence_penalty", "frequency_penalty", "max_tokens",
    )
    for param in forbidden:
        assert param not in request, f"reasoning models reject {param!r} (400s)"
    assert request["max_completion_tokens"] == _MAX_COMPLETION_TOKENS


def test_extract_image_sends_image_url_block_before_text():
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    asyncio.run(
        adapter.extract_image(
            system_prompt="SYSTEM", user_prompt="USER", image_png_b64="QUJD", schema_json=_SCHEMA
        )
    )
    (request,) = client.chat.completions.requests
    # messages[0] is the system turn; messages[1] is the user turn whose
    # content list carries the image ahead of the instructions.
    image_block, text_block = request["messages"][1]["content"]
    assert image_block == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    assert text_block == {"type": "text", "text": "USER"}


def test_supports_vision_and_provider_id():
    adapter, _ = _adapter(_response(json.dumps(_PAYLOAD)))
    assert adapter.supports_vision is True
    assert adapter.provider_id == "openai"


def test_code_fenced_json_is_recovered_without_burning_the_repair():
    fenced = "```json\n" + json.dumps(_PAYLOAD) + "\n```"
    adapter, _ = _adapter(_response(fenced))
    result = asyncio.run(
        adapter.extract_text(system_prompt="S", user_prompt="U", schema_json=_SCHEMA)
    )
    assert result.raw_json == _PAYLOAD


def test_response_without_message_content_raises_for_the_repair_path():
    """OpenAI's refusal/empty-completion shape is an empty `message.content`
    (finish_reason explains why), not Anthropic's empty `content` block
    list -- same failure CLASS (surfaced so the bounded-repair path owns
    it), different provider shape."""
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None), finish_reason="content_filter")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=0, prompt_tokens_details=None),
    )
    adapter, _ = _adapter(response)
    with pytest.raises(ValueError, match="no message content"):
        asyncio.run(adapter.extract_text(system_prompt="S", user_prompt="U", schema_json=_SCHEMA))


def test_repair_is_schema_bound_complete_json_is_not():
    """repair and complete_json share the same underlying _call plumbing
    (message shape, no sampling params, same token cap) but differ in
    response_format, exactly matching base.py's ABC contract: repair is
    still schema-bound, complete_json is explicitly free-form (never passed
    a schema)."""
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    asyncio.run(adapter.repair(system_prompt="S", user_prompt="FIX", schema_json=_SCHEMA))
    asyncio.run(adapter.complete_json(system_prompt="S", user_prompt="FREE"))

    repair_request, complete_request = client.chat.completions.requests
    assert repair_request["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "passage_extraction", "schema": _SCHEMA, "strict": False},
    }
    assert complete_request["response_format"] == {"type": "json_object"}
    for request in (repair_request, complete_request):
        assert "temperature" not in request
        assert request["max_completion_tokens"] == _MAX_COMPLETION_TOKENS


def test_missing_cached_token_details_defaults_to_zero():
    """Defensive fallback exercised: a response carrying no
    prompt_tokens_details at all must normalize cached_input_tokens to 0,
    never raise (the adapter's own getattr-guarded path, not assumed)."""
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_PAYLOAD)), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None),
    )
    adapter, _ = _adapter(response)
    result = asyncio.run(
        adapter.extract_text(system_prompt="S", user_prompt="U", schema_json=_SCHEMA)
    )
    assert result.usage.cached_input_tokens == 0
