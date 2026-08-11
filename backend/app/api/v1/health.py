"""Liveness endpoint -- infra only, no business logic. Copied from UC2
(which copied it from UC1)."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
