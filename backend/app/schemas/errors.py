"""Pydantic models for the error envelope (Handbook §6.2: accurate OpenAPI
docs). Mirrors the shape core/errors.py's handlers actually emit -- copied
from UC2's envelope contract (`{"error": {"type", "message", "details?"}}`)
so the OpenAPI error responses document the real wire format."""

from __future__ import annotations

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    type: str
    message: str
    details: dict | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
