"""Scripted ModelClient double (docs/plan.md D2 dev-time story): the runner
is exercised end to end -- trace persistence, budgets, gates, resume --
without a single live token. Tests script the exact tool sequence each
agent kind should walk; the keyless demo can replay the same scripts.

Deliberately dumb: it never inspects tool results or messages, it just
plays the next scripted turn. Realism lives in the scripts, determinism
lives here.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from app.services.agents.model_client import ModelToolCall, ModelTurn

_ids = itertools.count(1)


@dataclass(frozen=True)
class ScriptedToolUse:
    name: str
    args: dict


@dataclass(frozen=True)
class ScriptedTurn:
    """One assistant turn: tool uses (=> stop_reason tool_use) or final
    text (=> end_turn). Token counts are inputs so accounting tests can
    assert exact rollups against MODEL_PRICES arithmetic."""

    tool_uses: tuple[ScriptedToolUse, ...] = ()
    text: str = ""
    input_tokens: int = 500
    output_tokens: int = 80


@dataclass
class FakeModelClient:
    turns: list[ScriptedTurn]
    calls: list[dict] = field(default_factory=list)
    is_scripted: bool = True  # the approval endpoint resumes these inline

    def create_turn(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
    ) -> ModelTurn:
        self.calls.append(
            {
                "model": model,
                "tools": [t["function"]["name"] for t in tools],
                "message_count": len(messages),
            }
        )
        if not self.turns:
            # A script that under-provisioned turns ends the run cleanly
            # instead of crashing the loop -- the trace shows what happened.
            return ModelTurn(
                stop_reason="end_turn",
                text="(script exhausted)",
                tool_calls=(),
                input_tokens=0,
                output_tokens=0,
                raw_content=[{"type": "text", "text": "(script exhausted)"}],
            )
        scripted = self.turns.pop(0)
        tool_calls = tuple(
            ModelToolCall(id=f"toolu_fake_{next(_ids)}", name=use.name, input=use.args)
            for use in scripted.tool_uses
        )
        raw_content: list[dict] = []
        if scripted.text:
            raw_content.append({"type": "text", "text": scripted.text})
        raw_content.extend(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
            for call in tool_calls
        )
        return ModelTurn(
            stop_reason="tool_use" if tool_calls else "end_turn",
            text=scripted.text,
            tool_calls=tool_calls,
            input_tokens=scripted.input_tokens,
            output_tokens=scripted.output_tokens,
            raw_content=raw_content,
        )
