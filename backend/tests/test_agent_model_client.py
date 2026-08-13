"""OpenAIModelClient contract tests -- the one class in the whole Anthropic
to OpenAI swap (docs/plan.md D3, revised 2026-08-12) with no coverage
anywhere else in the suite: every scripted-agent test exercises
FakeModelClient instead, never the real HTTP-calling class or its four
private IR<->OpenAI wire-format translation helpers. Those helpers are the
single most novel, hand-built code in the swap and exactly what a live
keyed run hits first -- this file closes that gap with zero network calls
and no API key, by swapping a SimpleNamespace-based fake onto the
already-constructed client's `_client` attribute (mirrors
test_extraction_openai_adapter.py's fake-response idiom) and driving the
real `create_turn` end to end.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.agents.model_client import (
    OpenAIModelClient,
    real_model_client_or_none,
    tool_result_block,
)


def _client() -> OpenAIModelClient:
    """A real OpenAIModelClient with a fake api_key -- __init__ only ever
    constructs the SDK client object (no network call until create_turn),
    so this is safe without a real key. azure_endpoint/api_version are
    required by AzureOpenAI's constructor (2026-08-13 revision: this
    client's key is an Azure AI Foundry resource key, not a
    platform.openai.com key -- config.py's OpenAI settings block has the
    full story) but never dialed since _client gets swapped for a fake."""
    return OpenAIModelClient(
        "sk-fake-never-used", "https://unused.example.com", "2025-04-01-preview"
    )


def _fake_completions(response):
    """Swap the real SDK call for a canned response, capturing the exact
    kwargs create_turn passed -- lets tests assert on the translated
    request shape, not just the parsed result."""
    captured: dict = {}

    def create(**kwargs):
        captured["kwargs"] = kwargs
        return response

    return create, captured


def _response(*, text=None, tool_calls=None, finish_reason="stop", refusal=None,
              prompt_tokens=10, completion_tokens=5, cached_tokens=None):
    details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens is not None else None
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=tool_calls, refusal=refusal),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=details,
        ),
    )


def _tool_call(id_, name, arguments):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=arguments))


# --- request shape: no sampling params, max_completion_tokens, system-first -


def test_create_turn_never_sends_sampling_params():
    client = _client()
    create, captured = _fake_completions(_response(text="hi"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client.create_turn(model="gpt-5.5", system="be terse", tools=[], messages=[
        {"role": "user", "content": "start"},
    ])
    kwargs = captured["kwargs"]
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "logprobs" not in kwargs
    assert "max_tokens" not in kwargs  # legacy field must never be sent
    assert kwargs["max_completion_tokens"] == 8_000
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
    assert kwargs["messages"][1] == {"role": "user", "content": "start"}


# --- text-only turn -----------------------------------------------------


def test_text_only_turn_maps_to_end_turn():
    client = _client()
    create, _ = _fake_completions(
        _response(text="the answer is 42", finish_reason="stop", prompt_tokens=100, completion_tokens=8)
    )
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[
        {"role": "user", "content": "q"},
    ])
    assert turn.stop_reason == "end_turn"
    assert turn.text == "the answer is 42"
    assert turn.tool_calls == ()
    assert turn.input_tokens == 100
    assert turn.output_tokens == 8
    assert turn.cached_input_tokens == 0  # no prompt_tokens_details at all
    assert turn.raw_content == [{"type": "text", "text": "the answer is 42"}]


# --- tool-call turn: parsing, argument decoding, raw_content shape ------


def test_tool_call_turn_parses_arguments_and_ids():
    client = _client()
    calls = [
        _tool_call("call_1", "get_passage_text", json.dumps({"passage_id": "abc"})),
        _tool_call("call_2", "decide", json.dumps({"decision": "merge"})),
    ]
    create, _ = _fake_completions(_response(text=None, tool_calls=calls, finish_reason="tool_calls"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.stop_reason == "tool_use"
    assert turn.text == ""
    assert [c.name for c in turn.tool_calls] == ["get_passage_text", "decide"]
    assert turn.tool_calls[0].input == {"passage_id": "abc"}
    assert turn.tool_calls[0].id == "call_1"
    assert turn.raw_content == [
        {"type": "tool_use", "id": "call_1", "name": "get_passage_text", "input": {"passage_id": "abc"}},
        {"type": "tool_use", "id": "call_2", "name": "decide", "input": {"decision": "merge"}},
    ]


def test_malformed_tool_arguments_degrade_to_empty_dict_not_a_crash():
    client = _client()
    calls = [_tool_call("call_1", "decide", "{not valid json")]
    create, _ = _fake_completions(_response(tool_calls=calls, finish_reason="tool_calls"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.tool_calls[0].input == {}


def test_non_dict_json_arguments_also_degrade_to_empty_dict():
    client = _client()
    calls = [_tool_call("call_1", "decide", json.dumps(["not", "an", "object"]))]
    create, _ = _fake_completions(_response(tool_calls=calls, finish_reason="tool_calls"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.tool_calls[0].input == {}


# --- stop-reason mapping: every branch ----------------------------------


@pytest.mark.parametrize(
    ("finish_reason", "refusal", "expected"),
    [
        ("tool_calls", None, "tool_use"),
        ("stop", None, "end_turn"),
        ("length", None, "max_tokens"),
        ("content_filter", None, "refusal"),
        (None, None, "end_turn"),  # unrecognized/absent fails safe, never crashes
        ("some_future_value", None, "end_turn"),
        ("stop", "the model declined to answer", "refusal"),  # refusal wins regardless
        ("tool_calls", "declined", "refusal"),
    ],
)
def test_stop_reason_mapping_every_branch(finish_reason, refusal, expected):
    client = _client()
    create, _ = _fake_completions(_response(text="x", finish_reason=finish_reason, refusal=refusal))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.stop_reason == expected


def test_message_without_a_refusal_attribute_at_all_is_handled():
    """Real SDK response objects always have .refusal, but the code reads
    it via getattr with a default specifically because that was never
    verified against a live call -- prove the defensive path actually
    works against an object that lacks the attribute entirely."""
    client = _client()
    message = SimpleNamespace(content="hi", tool_calls=None)  # no .refusal at all
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None),
    )
    create, _ = _fake_completions(response)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.stop_reason == "end_turn"


# --- cached-token defensive fallback -------------------------------------


def test_cached_tokens_read_when_present():
    client = _client()
    create, _ = _fake_completions(_response(text="x", cached_tokens=42))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.cached_input_tokens == 42


def test_cached_tokens_default_to_zero_when_details_absent():
    client = _client()
    create, _ = _fake_completions(_response(text="x"))  # prompt_tokens_details=None
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[])
    assert turn.cached_input_tokens == 0


# --- IR -> OpenAI wire-shape translation: the novel, hand-built part ----


def test_assistant_turn_with_tool_calls_translates_to_openai_shape():
    """runner.py echoes an assistant turn back as
    {'role':'assistant','content': list(turn.raw_content)} -- prove that
    round-trips through create_turn's _to_openai_messages into OpenAI's
    top-level tool_calls field, not left inline in content."""
    client = _client()
    create, captured = _fake_completions(_response(text="ok"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    ir_messages = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "call_1", "name": "sniff_type", "input": {"document_id": "d1"}},
            ],
        },
    ]
    client.create_turn(model="gpt-5.5", system="s", tools=[], messages=ir_messages)
    sent = captured["kwargs"]["messages"]
    assistant_msg = next(m for m in sent if m.get("role") == "assistant")
    assert assistant_msg["content"] == "let me check"
    assert assistant_msg["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "sniff_type", "arguments": json.dumps({"document_id": "d1"})},
        }
    ]


def test_assistant_turn_with_only_tool_calls_has_null_content():
    client = _client()
    create, captured = _fake_completions(_response(text="ok"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    ir_messages = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "c1", "name": "sniff_type", "input": {}}],
        },
    ]
    client.create_turn(model="gpt-5.5", system="s", tools=[], messages=ir_messages)
    assistant_msg = next(m for m in captured["kwargs"]["messages"] if m.get("role") == "assistant")
    assert assistant_msg["content"] is None


def test_bundled_tool_results_unbundle_to_one_message_each():
    """tool_result_block bundles N results into one IR 'user' turn the way
    Anthropic wanted them; OpenAI needs one role:'tool' message PER result
    -- prove create_turn actually unbundles, not just accepts the shape."""
    client = _client()
    create, captured = _fake_completions(_response(text="ok"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    bundled_turn = {
        "role": "user",
        "content": [
            tool_result_block("call_1", {"ok": True}, is_error=False),
            tool_result_block("call_2", {"error": "bad args"}, is_error=True),
        ],
    }
    client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[bundled_turn])
    tool_msgs = [m for m in captured["kwargs"]["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert json.loads(tool_msgs[0]["content"]) == {"ok": True}
    assert tool_msgs[1]["tool_call_id"] == "call_2"
    assert json.loads(tool_msgs[1]["content"]) == {"error": "bad args"}


def test_image_tool_result_re_expands_into_a_separate_user_message():
    """get_page_image's vision path: tool_result_block turns an
    `_image_base64` payload into an IR image block bundled with the
    request's other results; OpenAI's role:'tool' messages are text-only,
    so create_turn must re-expand the image into its own role:'user'
    image_url message immediately after the tool's text result."""
    client = _client()
    create, captured = _fake_completions(_response(text="ok"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    block = tool_result_block(
        "call_img", {"page": 0, "_image_base64": "QUJD", "media_type": "image/png"}, is_error=False
    )
    bundled_turn = {"role": "user", "content": [block]}
    client.create_turn(model="gpt-5.5", system="s", tools=[], messages=[bundled_turn])
    sent = captured["kwargs"]["messages"]
    tool_idx = next(i for i, m in enumerate(sent) if m.get("role") == "tool")
    assert sent[tool_idx]["tool_call_id"] == "call_img"
    # only the private _image_base64 key is popped out of the payload --
    # media_type legitimately stays in the text alongside the rest.
    assert json.loads(sent[tool_idx]["content"]) == {"page": 0, "media_type": "image/png"}
    image_msg = sent[tool_idx + 1]
    assert image_msg["role"] == "user"
    assert image_msg["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    ]


def test_full_two_tool_call_turn_round_trip():
    """A complete opening prompt -> assistant tool_use turn -> bundled
    tool_result turn, exactly the shape runner.py's loop actually builds
    across two real turns -- proves the whole translation pipeline agrees
    with itself end to end, not just each helper in isolation."""
    client = _client()
    create, captured = _fake_completions(_response(text="done", finish_reason="stop"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    messages = [
        {"role": "user", "content": "investigate quarantine q1"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "get_document_meta", "input": {"document_id": "d1"}},
                {"type": "tool_use", "id": "c2", "name": "sniff_type", "input": {"document_id": "d1"}},
            ],
        },
        {
            "role": "user",
            "content": [
                tool_result_block("c1", {"file_class": "pdf_digital"}, is_error=False),
                tool_result_block("c2", {"sniffed": "application/pdf"}, is_error=False),
            ],
        },
    ]
    turn = client.create_turn(model="gpt-5.5", system="you are the investigator", tools=[], messages=messages)
    assert turn.stop_reason == "end_turn"
    sent = captured["kwargs"]["messages"]
    assert sent[0] == {"role": "system", "content": "you are the investigator"}
    assert sent[1] == {"role": "user", "content": "investigate quarantine q1"}
    assistant_msg = next(m for m in sent if m.get("role") == "assistant")
    assert len(assistant_msg["tool_calls"]) == 2
    tool_msgs = [m for m in sent if m.get("role") == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}


# --- construction / gating -----------------------------------------------


def test_construction_makes_no_network_call():
    """__init__ only builds the SDK client object; no request is issued
    until create_turn is actually called."""
    client = OpenAIModelClient(
        "sk-fake-never-used", "https://unused.example.com", "2025-04-01-preview"
    )
    assert client.is_scripted is False
    assert client._client is not None  # constructed, but nothing was sent


def test_real_model_client_or_none_gates_on_settings(monkeypatch):
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    config_module.get_settings.cache_clear()
    assert real_model_client_or_none() is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-present")
    config_module.get_settings.cache_clear()
    client = real_model_client_or_none()
    assert isinstance(client, OpenAIModelClient)

    monkeypatch.setenv("OPENAI_API_KEY", "")
    config_module.get_settings.cache_clear()
