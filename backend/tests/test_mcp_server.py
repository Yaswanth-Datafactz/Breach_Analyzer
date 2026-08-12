"""MCP facade tests (docs/plan.md D4): the stdio server exposes the SAME
registry -- schemas auto-derived from the Pydantic args models, read-only
tools by default, mutating tools only behind MCP_ALLOW_MUTATIONS, one DB
session per call.

Requires the docker-compose Postgres on :5434 with migrations applied
(the call-through test reads a real passage row).
"""

from __future__ import annotations

import pytest

from app.db.session import SessionLocal
from app.services.agents.tools import REGISTRY
from mcp_server import build_server
from tests.agent_env import build_agent_env, teardown_agent_env

READ_ONLY = {name for name, spec in REGISTRY.items() if not spec.mutating}
MUTATING = {name for name, spec in REGISTRY.items() if spec.mutating}


@pytest.fixture(scope="module")
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture(scope="module")
def env(db):
    environment = build_agent_env(db)
    yield environment
    teardown_agent_env(db, environment)


def test_default_server_exposes_read_only_tools_only():
    server = build_server(allow_mutations=False)
    exposed = {tool.name for tool in server._tool_manager.list_tools()}
    assert exposed == READ_ONLY
    assert "decide" not in exposed
    assert "resolve_quarantine" not in exposed
    assert "request_approval" not in exposed


def test_mutations_flag_exposes_the_full_registry():
    server = build_server(allow_mutations=True)
    exposed = {tool.name for tool in server._tool_manager.list_tools()}
    assert exposed == READ_ONLY | MUTATING == set(REGISTRY)
    decide = server._tool_manager.get_tool("decide")
    assert decide.description.startswith("[MUTATING]")


def test_schemas_are_derived_from_the_pydantic_args_models():
    server = build_server(allow_mutations=True)
    for name, spec in REGISTRY.items():
        tool = server._tool_manager.get_tool(name)
        expected = set(spec.args_model.model_fields)
        assert set(tool.parameters["properties"]) == expected, name
    # spot-check constraints survive derivation
    ocr = server._tool_manager.get_tool("run_ocr").parameters
    assert ocr["properties"]["dpi"]["maximum"] == 400
    assert set(ocr["required"]) == {"document_id"}


def test_read_only_call_round_trips_with_its_own_session(env):
    server = build_server(allow_mutations=False)
    tool = server._tool_manager.get_tool("get_passage_text")
    result = tool.fn(passage_id=str(env.passage_flag.id))
    assert result["is_error"] is False
    assert "523-88-1234" in result["text"]
    missing = tool.fn(passage_id="00000000-0000-0000-0000-000000000000")
    assert missing["is_error"] is True


def test_gate_tool_without_agent_context_errors_cleanly(env):
    server = build_server(allow_mutations=True)
    tool = server._tool_manager.get_tool("request_approval")
    result = tool.fn(action_type="final_signoff", payload={})
    assert result["is_error"] is True
    assert "agent-run context" in result["error"]
