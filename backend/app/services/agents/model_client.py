"""The model-client seam (docs/plan.md D2: first-party AgentRunner over
native tool use). The runner depends on this interface only -- tests inject
fake_client.FakeModelClient; production wires OpenAIModelClient, which is
CONSTRUCTED ONLY WHEN A KEY EXISTS (real_model_client_or_none). No module in
this package ever makes a live call implicitly.

Revised 2026-08-12 (docs/plan.md D3 swap): Anthropic replaced with OpenAI --
no Anthropic key is available in this environment, only an OpenAI one; see
core/config.py's OpenAI settings block for the model/pricing citation.
REVISED AGAIN 2026-08-13: that OpenAI key is an Azure AI Foundry resource
key (the same resource DeepSeek uses), not a platform.openai.com key --
confirmed live. The client is `openai.AzureOpenAI`, not `openai.OpenAI`,
constructed with `azure_endpoint`/`api_version` from config alongside the
key; `chat.completions.create`'s call shape and this module's whole IR
translation are UNCHANGED, since Azure's classic deployments API is the
same Chat-Completions shape as the real one (config.py's OpenAI settings
block has the full endpoint-discovery story).
OpenAI specifics kept in this one adapter:
- agent model comes from config (`agent_model`, "gpt-5.5" -- the one
  deployment that exists on this Foundry resource; per-agent downgrade to
  a second deployment is a config change -- docs/plan.md D3).
- gpt-5.5 is a REASONING model: the adapter passes NO sampling params
  (temperature/top_p are rejected with a 400, same shape opus-5's thinking
  mode had -- confirmed live against the real deployment, and against
  OpenAI's reasoning-models guide, see extraction/openai_adapter.py's
  docstring for the fuller citation) and uses `max_completion_tokens`, not
  the legacy `max_tokens`.
- prompt caching is automatic and server-side for OpenAI -- there is no
  cache_control breakpoint to set (unlike the retired Anthropic adapter);
  simply gone, not replaced (same note as extraction/openai_adapter.py).
- this codebase's `messages` list (built by runner.py) and `tool_result_block`
  below are a provider-neutral wire IR, NOT OpenAI's actual wire shape:
  content is a list of {"type": "text"|"tool_use"|"tool_result", ...}
  blocks, the same shape fake_client.py's raw_content already produces.
  runner.py only ever treats it as an opaque list to echo back
  (`{"role": ..., "content": list(turn.raw_content)}`) or build
  (`tool_result_block(...)`) -- it never inspects a block's internals, so
  translating that IR to/from OpenAI's real wire shape (assistant
  `tool_calls` as a top-level field alongside `content`, one `role: "tool"`
  message per result rather than one user turn bundling N results) is
  entirely this class's job, contained in create_turn. runner.py, tools.py,
  and fake_client.py need no knowledge of OpenAI's actual message shape.
- `raw_content` on the turns THIS client returns is hand-built plain dicts,
  not passed-through SDK objects (unlike the retired Anthropic adapter,
  which could echo `response.content` blocks verbatim) -- OpenAI's own
  response shape doesn't match this IR, so it is synthesized here.
- a tool result payload carrying `_image_base64` (the get_page_image ->
  vision path) becomes an IR image block in tool_result_block, same as
  before; this client re-expands it into a separate image-carrying user
  message, since OpenAI's `role: "tool"` messages are text-only.
- OpenAI's structured-outputs-era `message.refusal` (populated when the
  model declines to comply) is this provider's analog of Anthropic's
  stop_reason == "refusal" and is surfaced as a turn, never an exception --
  the runner records it and fails the run cleanly, exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from app.core.config import get_settings

# Output headroom per turn (OpenAI's `max_completion_tokens`: reasoning
# tokens and visible text/tool-call output share this one budget, unlike
# the legacy `max_tokens` field reasoning models reject -- see
# extraction/openai_adapter.py's docstring for the fuller citation). Agent
# turns are short tool calls -- 8K keeps a single turn well under every
# per-run USD budget while never truncating a plan. UNVERIFIED against a
# live call (no key in this environment): a reasoning model's hidden
# reasoning tokens draw from this same budget before any visible output,
# so if live turns start finishing with stop_reason == "max_tokens" on
# ordinary tool calls, raise this before assuming anything else is wrong.
_MAX_COMPLETION_TOKENS = 8_000

_REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ModelTurn:
    """One assistant turn, provider-neutral. `raw_content` is the exact
    content-block list to echo back as the assistant message (plain dicts
    from both the real client and the fakes -- see module docstring)."""

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
    """This codebase's provider-neutral tool_result IR block (module
    docstring): one turn's N tool results are bundled here exactly as
    Anthropic's Messages API wants them, and unbundled into OpenAI's
    one-message-per-result shape inside OpenAIModelClient.create_turn.
    `_image_base64` in the payload becomes an image content block so
    vision-capable tools (get_page_image) put pixels in front of the
    model, not a base64 string to 'read'."""
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


_STOP_REASON_MAP = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
}


def _map_stop_reason(finish_reason: str | None, message: Any) -> str:
    """OpenAI finish_reason -> this codebase's provider-neutral vocabulary
    (ModelTurn's docstring: 'tool_use' | 'end_turn' | 'max_tokens' |
    'refusal'). runner.py's only string-equality check against this value
    is 'refusal'; every other branch keys off tool_calls truthiness, so an
    unrecognized finish_reason fails safe into 'end_turn' (-> the contract-
    parse path) rather than crashing the loop.

    message.refusal is checked first, independent of finish_reason -- it
    is OpenAI's structured-outputs-era refusal signal, the direct analog
    of Anthropic's stop_reason == 'refusal'. getattr with a default because
    its presence on every SDK response shape was not re-verified against a
    live call (no key in this environment); absent-and-falsy is the safe
    default and costs nothing if the field turns out not to apply here.
    """
    if getattr(message, "refusal", None):
        return "refusal"
    if finish_reason == "content_filter":
        return "refusal"
    return _STOP_REASON_MAP.get(finish_reason or "", "end_turn")


def _parse_tool_arguments(raw: str) -> dict:
    """OpenAI sends tool-call arguments as a JSON-encoded string (Anthropic
    sent an already-parsed dict). Malformed JSON from the model degrades to
    an empty dict rather than crashing the run loop -- run_tool's Pydantic
    validation (tools.py) then reports it as a normal invalid-arguments
    tool error, the same failure shape a legitimately-wrong argument set
    already produces."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _assistant_message(blocks: Sequence[dict]) -> dict:
    """IR assistant blocks -> one OpenAI assistant message. OpenAI carries
    tool calls as a top-level `tool_calls` field alongside `content`
    (never inline in content the way Anthropic's tool_use blocks are)."""
    text_parts = [block["text"] for block in blocks if block.get("type") == "text"]
    calls = [block for block in blocks if block.get("type") == "tool_use"]
    message: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
    if calls:
        message["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["input"], default=str),
                },
            }
            for call in calls
        ]
    return message


def _tool_result_messages(blocks: Sequence[dict]) -> list[dict]:
    """One turn's bundled IR tool_result blocks -> OpenAI's one
    `role: "tool"` message PER result (OpenAI has no equivalent of
    Anthropic bundling N results into a single user turn). An image block
    (get_page_image -> vision) rides back as a separate `role: "user"`
    image_url message immediately after its tool's text result, since
    OpenAI's tool-role messages are text-only."""
    out: list[dict] = []
    for block in blocks:
        content = block["content"]
        if isinstance(content, list):  # image + text, per tool_result_block
            text = next((c["text"] for c in content if c.get("type") == "text"), "")
            out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
            image = next((c for c in content if c.get("type") == "image"), None)
            if image is not None:
                media_type = image["source"].get("media_type", "image/png")
                data = image["source"]["data"]
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"},
                            }
                        ],
                    }
                )
        else:
            out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": content})
    return out


def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the provider-neutral IR runner.py builds into OpenAI's
    wire shape. `system` is a bare string -- no cache_control breakpoint to
    carry (module docstring: OpenAI's prompt caching is automatic)."""
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        role = message["role"]
        if isinstance(content, str):  # the run's opening user prompt only
            out.append({"role": role, "content": content})
        elif role == "assistant":
            out.append(_assistant_message(content))
        else:  # role == "user": always tool_result IR blocks in this codebase
            out.extend(_tool_result_messages(content))
    return out


class OpenAIModelClient:
    """Real adapter over the openai SDK's synchronous client
    (chat.completions.create with `tools=[...]` for function/tool calling
    -- see module docstring for the IR translation this class owns). openai
    is imported lazily inside __init__, so this module stays importable
    with no SDK/key present at all; constructing a real client (which only
    happens when OPENAI_API_KEY is set) is the only time the import cost is
    paid, and no network call happens until create_turn."""

    is_scripted = False

    def __init__(self, api_key: str, azure_endpoint: str, api_version: str):
        import openai

        self._client = openai.AzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    def create_turn(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
    ) -> ModelTurn:
        response = self._client.chat.completions.create(
            model=model,
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
            tools=tools,
            messages=_to_openai_messages(system, messages),
            # No temperature/top_p -- gpt-5.5 is a reasoning model and
            # rejects sampling params (core/config.py).
        )
        choice = response.choices[0]
        message = choice.message
        text = message.content or ""
        tool_calls: list[ModelToolCall] = []
        raw_content: list[dict] = []
        if text:
            raw_content.append({"type": "text", "text": text})
        for call in message.tool_calls or []:
            tool_input = _parse_tool_arguments(call.function.arguments)
            tool_calls.append(ModelToolCall(id=call.id, name=call.function.name, input=tool_input))
            raw_content.append(
                {"type": "tool_use", "id": call.id, "name": call.function.name, "input": tool_input}
            )
        usage = response.usage
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", None) or 0
        return ModelTurn(
            stop_reason=_map_stop_reason(choice.finish_reason, message),
            text=text,
            tool_calls=tuple(tool_calls),
            # OpenAI's prompt_tokens already INCLUDES cached tokens (no
            # separate cache-write figure to fold, unlike the retired
            # Anthropic adapter -- same note as extraction/openai_adapter.py).
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cached_input_tokens=cached,
            raw_content=raw_content,
        )


def real_model_client_or_none() -> OpenAIModelClient | None:
    """The single gate every live-dispatch path goes through: no
    OPENAI_API_KEY -> None (callers map that to a 409 or a logged no-op,
    never a crash)."""
    settings = get_settings()
    if not settings.openai_api_key:
        return None
    return OpenAIModelClient(settings.openai_api_key, settings.openai_base_url, settings.openai_api_version)
