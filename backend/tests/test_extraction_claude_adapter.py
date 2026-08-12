"""ClaudeExtractionAdapter contract tests with a FAKE client object (the
SDK client's `messages.create` surface) -- request shape and usage
normalization are verified without any live call, mirroring deepseek.py's
MockTransport pattern. Verifies: prompt-caching cache_control on the
stable system prefix (plan §9), usage normalized to the ExtractionUsage
convention (input INCLUDES cached; cached reported separately), vision
content blocks, fence-stripping recovery, and the no-text-block failure."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services.extraction.claude import ClaudeExtractionAdapter

_PAYLOAD = {"mentions": [], "elements": []}


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _response(text, *, input_tokens=100, output_tokens=50, cache_read=30, cache_creation=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        ),
        stop_reason="end_turn",
    )


def _adapter(response):
    client = _FakeClient(response)
    return ClaudeExtractionAdapter(api_key="unused", model="claude-sonnet-4-6", client=client), client


def test_extract_text_caches_stable_system_prefix_and_normalizes_usage():
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    result = asyncio.run(
        adapter.extract_text(system_prompt="SYSTEM", user_prompt="USER", schema_json={})
    )

    (request,) = client.messages.requests
    assert request["model"] == "claude-sonnet-4-6"
    # the stable prefix carries the cache breakpoint (plan §9 cache rule)
    (system_block,) = request["system"]
    assert system_block["text"] == "SYSTEM"
    assert system_block["cache_control"] == {"type": "ephemeral"}
    assert request["messages"] == [{"role": "user", "content": [{"type": "text", "text": "USER"}]}]

    assert result.raw_json == _PAYLOAD
    # Anthropic reports uncached input; ExtractionUsage wants the total.
    assert result.usage.input_tokens == 100 + 30 + 10
    assert result.usage.cached_input_tokens == 30
    assert result.usage.output_tokens == 50
    assert result.logprobs is None  # the API exposes none -- composite renormalizes


def test_extract_image_sends_base64_png_block_before_text():
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    asyncio.run(
        adapter.extract_image(
            system_prompt="SYSTEM", user_prompt="USER", image_png_b64="QUJD", schema_json={}
        )
    )
    (request,) = client.messages.requests
    image_block, text_block = request["messages"][0]["content"]
    assert image_block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }
    assert text_block == {"type": "text", "text": "USER"}


def test_supports_vision_and_provider_id():
    adapter, _ = _adapter(_response(json.dumps(_PAYLOAD)))
    assert adapter.supports_vision is True
    assert adapter.provider_id == "anthropic"


def test_code_fenced_json_is_recovered_without_burning_the_repair():
    fenced = "```json\n" + json.dumps(_PAYLOAD) + "\n```"
    adapter, _ = _adapter(_response(fenced))
    result = asyncio.run(
        adapter.extract_text(system_prompt="S", user_prompt="U", schema_json={})
    )
    assert result.raw_json == _PAYLOAD


def test_response_without_text_block_raises_for_the_repair_path():
    response = SimpleNamespace(
        content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=0), stop_reason="refusal"
    )
    adapter, _ = _adapter(response)
    with pytest.raises(ValueError, match="no text block"):
        asyncio.run(adapter.extract_text(system_prompt="S", user_prompt="U", schema_json={}))


def test_repair_and_complete_json_share_the_call_shape():
    adapter, client = _adapter(_response(json.dumps(_PAYLOAD)))
    asyncio.run(adapter.repair(system_prompt="S", user_prompt="FIX", schema_json={}))
    asyncio.run(adapter.complete_json(system_prompt="S", user_prompt="FREE"))
    assert len(client.messages.requests) == 2
    assert all(r["system"][0]["cache_control"] == {"type": "ephemeral"} for r in client.messages.requests)
