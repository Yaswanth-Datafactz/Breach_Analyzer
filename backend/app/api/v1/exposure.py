"""GET /exposure (paginate/search/filter), GET /persons/{id} -- the exposure table itself (docs/plan.md §5's API surface).

Mounted empty so the /api/v1 surface and auth semantics are stable from day
one -- no fake endpoints before the behavior behind them exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_api_key
from app.schemas.errors import ErrorEnvelope

router = APIRouter(
    prefix="/exposure",
    tags=["exposure"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"description": "Missing or invalid API key", "model": ErrorEnvelope}},
)

# routes land with their service in phase B1/B2
