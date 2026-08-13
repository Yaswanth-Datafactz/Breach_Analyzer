"""The agent layer (docs/plan.md §3): four budgeted, fully-traced agents
beside the pipeline, never inside the per-document loop.

Module map:
- tools.py        -- the single typed tool registry (Pydantic args models);
                     both facades derive from it (native OpenAI tool defs
                     for in-app agents, FastMCP for backend/mcp_server.py).
- runner.py       -- the generic AgentRunner loop (~200 lines): trace-before-
                     execute, budgets between turns, approval-gate parking.
- budgets.py      -- Budget arithmetic + per-kind config defaults.
- traces.py       -- agent_steps / agent_tool_calls / cost_events persistence
                     with the commit-before-execute contract.
- model_client.py -- the ModelClient interface + the real OpenAI adapter
                     (constructed only when OPENAI_API_KEY exists; docs/
                     plan.md D3 swap, 2026-08-12: Anthropic replaced with
                     OpenAI).
- fake_client.py  -- the scripted FakeModelClient (tests + keyless demos).
- orchestrator.py / investigator.py / adjudicator.py / auditor.py
                  -- per-agent system prompt, tool subset, budget defaults,
                     output-contract parsing, and trigger wiring.
"""
