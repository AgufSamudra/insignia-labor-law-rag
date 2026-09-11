from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

try:
    from .errors import (
        EmbeddingServiceError,
        InsigniaError,
        QueryServiceError,
        VectorStoreError,
    )
except ImportError:  # Supports running this module directly from src/.
    from errors import EmbeddingServiceError, InsigniaError, QueryServiceError, VectorStoreError


logger = logging.getLogger(__name__)


async def insignia_error_handler(
    request: Request, error: InsigniaError
) -> JSONResponse:
    """Return a safe JSON response for an expected application failure."""
    logger.error(
        "Application error | method=%s path=%s error_type=%s detail=%s",
        request.method,
        request.url.path,
        type(error).__name__,
        error,
    )
    status_code = (
        502
        if isinstance(error, (EmbeddingServiceError, QueryServiceError, VectorStoreError))
        else 500
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": str(error),
            "error_type": type(error).__name__,
        },
    )


async def unhandled_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Log unexpected failures with a traceback and avoid exposing internals."""
    logger.error(
        "Unhandled error | method=%s path=%s",
        request.method,
        request.url.path,
        exc_info=(type(error), error, error.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Terjadi error internal pada server",
            "error_type": "InternalServerError",
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register centralized handlers on the FastAPI application."""
    app.add_exception_handler(InsigniaError, insignia_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
