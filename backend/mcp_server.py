"""FastMCP stdio facade over the agent tool registry (docs/plan.md D4).

Facade 2 of the one registry in app/services/agents/tools.py: every tool
schema below is AUTO-DERIVED from the same Pydantic args models the in-app
agents use natively -- one implementation, two protocol surfaces, zero
drift. Read-only tools are exposed by default; mutating tools (decide,
resolve_quarantine, escalate, the directive tools, request_approval,
verify_flag) are exposed only when MCP_ALLOW_MUTATIONS=1. Each call runs in
its own DB session; mutations commit on success and roll back on error.

Usage -- wire into Claude Code via .mcp.json at the repo root:

    {
      "mcpServers": {
        "breach-analyser": {
          "command": "backend/.venv/bin/python",
          "args": ["backend/mcp_server.py"],
          "env": { "MCP_ALLOW_MUTATIONS": "0" }
        }
      }
    }

Run manually for a smoke test: `backend/.venv/bin/python backend/mcp_server.py`
(speaks MCP over stdio; requires the docker-compose Postgres on :5434).
"""

from __future__ import annotations

import inspect
import os
from typing import Annotated, Callable

from mcp.server.fastmcp import FastMCP

from app.db.session import SessionLocal
from app.services.agents.tools import REGISTRY, ToolContext, ToolSpec

_IMAGE_OMITTED_NOTE = (
    "image omitted over MCP -- use run_ocr for the text, or the authenticated "
    "/documents/{id}/file endpoint for the original bytes"
)


def _wrapper_for(spec: ToolSpec) -> Callable:
    """A callable whose signature mirrors the tool's Pydantic args model, so
    FastMCP's schema derivation (which reads function signatures) reproduces
    the exact schema the native OpenAI facade publishes."""

    def wrapper(**kwargs):
        args = spec.args_model.model_validate(kwargs)
        db = SessionLocal()
        try:
            # MCP calls run outside any agent run: empty context. Gate tools
            # that need one (request_approval) answer with a typed error.
            result = spec.handler(db, args, ToolContext())
            if spec.mutating and not result.is_error:
                db.commit()
            else:
                db.rollback()
            payload = dict(result.payload)
            if payload.pop("_image_base64", None) is not None:
                payload["image"] = _IMAGE_OMITTED_NOTE
            return {"is_error": result.is_error, **payload}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    parameters = []
    annotations: dict[str, object] = {}
    for field_name, field in spec.args_model.model_fields.items():
        # Carry the field's constraint metadata (ge/le/min_length/...) into
        # the annotation so the derived MCP schema advertises the same
        # bounds the native OpenAI facade does.
        annotation: object = field.annotation
        for constraint in field.metadata:
            annotation = Annotated[annotation, constraint]
        annotations[field_name] = annotation
        parameters.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotation,
                default=(
                    inspect.Parameter.empty if field.is_required() else field.default
                ),
            )
        )
    wrapper.__name__ = spec.name
    wrapper.__doc__ = spec.description
    wrapper.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    wrapper.__annotations__ = annotations
    return wrapper


def build_server(*, allow_mutations: bool) -> FastMCP:
    server = FastMCP("breach-analyser")
    for spec in REGISTRY.values():
        if spec.mutating and not allow_mutations:
            continue
        description = spec.description
        if spec.mutating:
            description = f"[MUTATING] {description}"
        server.add_tool(_wrapper_for(spec), name=spec.name, description=description)
    return server


mcp = build_server(
    allow_mutations=os.environ.get("MCP_ALLOW_MUTATIONS", "").lower() in ("1", "true", "yes")
)


if __name__ == "__main__":
    mcp.run()
