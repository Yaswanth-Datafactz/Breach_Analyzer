"""The model-client seam (docs/plan.md D2: first-party AgentRunner over
Anthropic native tool use). The runner depends on this interface only --
tests inject fake_client.FakeModelClient; production wires
AnthropicModelClient, which is CONSTRUCTED ONLY WHEN A KEY EXISTS
(real_model_client_or_none). No module in this package ever makes a live
call implicitly.

Anthropic specifics kept in this one adapter:
- agent model comes from config (`agent_model`, claude-opus-5 by default;
  per-agent downgrade is a config change -- docs/plan.md D3).
- claude-opus-5 thinks by default; the adapter passes NO `thinking` config
  and NO sampling params (temperature/top_p/top_k are rejected with a 400
  on opus-5).
- the stable system prompt carries a cache_control breakpoint (prompt
  caching -- tools+system render first, volatile messages last).
- assistant content blocks are echoed back verbatim on the next turn
  (thinking blocks included -- editing them breaks the turn).
- tool results whose payload carries `_image_base64` become image content
  blocks (the get_page_image -> vision path).
- stop_reason 'refusal' is surfaced as a turn, never an exception -- the
  runner records it and fails the run cleanly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from app.core.config import get_settings

# Output headroom per turn: opus-5's max_tokens caps thinking + text
# together, and agent turns are short tool calls -- 8K keeps a single turn
# well under every per-run USD budget while never truncating a plan.
_MAX_TOKENS_PER_TURN = 8_000


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ModelTurn:
    """One assistant turn, provider-neutral. `raw_content` is the exact
    content-block list to echo back as the assistant message (SDK objects
    for the real client, plain dicts for fakes)."""

    stop_reason: str  # 'tool_use' | 'end_turn' | 'max_tokens' | 'refusal'
    text: str
    tool_calls: tuple[ModelToolCall, ...]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    raw_content: Sequence[Any] = field(default_factory=tuple)


class ModelClient(Protocol):
    """What the runner needs from a model: one turn at a time."""

    def create_turn(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
    ) -> ModelTurn: ...


def tool_result_block(tool_use_id: str, payload: dict, *, is_error: bool) -> dict:
    """Anthropic-shaped tool_result block. `_image_base64` in the payload
    becomes an image content block so vision-capable tools (get_page_image)
    put pixels in front of the model, not a base64 string to 'read'."""
    image_b64 = payload.pop("_image_base64", None)
    content: list[dict] | str
    text = json.dumps(payload, default=str)
    if image_b64:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": payload.get("media_type", "image/png"),
                    "data": image_b64,
                },
            },
            {"type": "text", "text": text},
        ]
    else:
        content = text
    block: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


class AnthropicModelClient:
    """Real adapter over the anthropic SDK. Import cost is paid at module
    import (the SDK is a pinned dependency); NETWORK cost is only ever paid
    inside create_turn, which nothing reaches without a key."""

    is_scripted = False

    def __init__(self, api_key: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def create_turn(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
    ) -> ModelTurn:
        response = self._client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS_PER_TURN,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=tools,
            messages=messages,
        )
        text_parts: list[str] = []
        tool_calls: list[ModelToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ModelToolCall(id=block.id, name=block.name, input=dict(block.input))
                )
        return ModelTurn(
            stop_reason=response.stop_reason or "end_turn",
            text="\n".join(text_parts),
            tool_calls=tuple(tool_calls),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            raw_content=list(response.content),
        )


def real_model_client_or_none() -> AnthropicModelClient | None:
    """The single gate every live-dispatch path goes through: no
    ANTHROPIC_API_KEY -> None (callers map that to a 409 or a logged no-op,
    never a crash)."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    return AnthropicModelClient(settings.anthropic_api_key)
