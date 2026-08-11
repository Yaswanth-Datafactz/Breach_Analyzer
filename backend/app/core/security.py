"""API-key auth dependency (Handbook §6.2: at least API-key authentication).

Copied verbatim from Document_Extraction/backend/app/core/security.py (UC2),
which itself copied it from UC1 -- third app, same auth surface.
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import APIKeyHeader

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: Annotated[str | None, Depends(_api_key_header)]) -> str:
    settings = get_settings()
    if not api_key or api_key != settings.api_key:
        raise UnauthorizedError("Missing or invalid API key")
    return api_key
