"""Centralized error handling (Handbook §6.2: never 200 with an error in the body).

Copied near-verbatim from Document_Extraction/backend/app/core/errors.py (UC2)
-- same error envelope (`{"error": {"type", "message", "details?"}}`), same
handler registration. UC2's upload-specific PayloadTooLargeError/
UnsupportedMediaTypeError are kept: UC3 ingests arbitrary corpus binaries and
serves originals, so the same status codes apply.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger("errors")


class AppError(Exception):
    """Base for application errors that map to a specific HTTP status code."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details
        super().__init__(message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_found"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "unauthorized"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "conflict"


class PayloadTooLargeError(AppError):
    """A single ingested file exceeded the configured size cap."""

    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    error_type = "payload_too_large"


class UnsupportedMediaTypeError(AppError):
    """The file's real content (magic bytes, not extension) isn't a supported
    corpus format -- see docs/plan.md §3: MIME-sniff vs extension at ingest;
    type is verified by content, never trusted from the filename."""

    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    error_type = "unsupported_media_type"


class ValidationFailedError(AppError):
    """A domain-level payload (review decision, approval decision, manifest
    import) failed validation. Distinct from FastAPI's own
    RequestValidationError (malformed request body)."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_type = "validation_failed"


def _error_body(error_type: str, message: str, details: dict | None = None) -> dict:
    body: dict = {"error": {"type": error_type, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error", path=request.url.path, error_type=exc.error_type, message=exc.message
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.error_type, exc.message, exc.details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        logger.warning("http_exception", path=request.url.path, status_code=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("validation_error", path=request.url.path, errors=exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_error_body("validation_error", "Request validation failed"),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_error", path=request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An unexpected error occurred"),
        )
