"""Error-handling plumbing for the FastAPI app (M16 T57, spec R1-R3, R9, R26, R28).

The legacy server shipped two slightly different envelope shapes
(``{"error": <msg>, "code": <code>}`` on some paths, ``{"error": <code>,
"message": <msg>}`` on others) — the migration spec (R9) normalizes
on the latter: ``{"error": "<CODE>", "message": "<human text>"}``.

All 4xx/5xx responses (except 204/304) emit this shape; FastAPI's
native 422 validation envelope is remapped to 400 ``BAD_REQUEST`` so
existing clients don't need to learn a new error format.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


# Canonical status → error-code mapping (spec R9).
_STATUS_TO_CODE: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_409_CONFLICT: "CONFLICT",
    416: "RANGE_NOT_SATISFIABLE",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "INTERNAL_ERROR",
}


def _code_for_status(status_code: int) -> str:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if 400 <= status_code < 500:
        return "BAD_REQUEST"
    return "INTERNAL_ERROR"


class ApiError(HTTPException):
    """``HTTPException`` that carries an explicit error code.

    Handlers can raise ``ApiError("PLUGIN_ERROR", "handler raised", 500)``
    to bypass the default status→code mapping — useful for domain
    codes like ``PLUGIN_ERROR`` that don't correspond to an HTTP
    status 1:1.
    """

    def __init__(self, code: str, message: str, status_code: int = 400, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def _envelope(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or _code_for_status(exc.status_code)
    # HTTPException.detail can be a string or a dict; normalize to text.
    if isinstance(exc.detail, str):
        message = exc.detail
    elif exc.detail is None:
        message = ""
    else:
        message = str(exc.detail)
    headers = getattr(exc, "headers", None) or None
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, message),
        headers=headers,
    )


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Flatten Pydantic validation errors to legacy 400 BAD_REQUEST envelope.

    Spec R26: preserve ``{"error": "BAD_REQUEST", "message": ...}`` — do
    NOT leak FastAPI's default ``{"detail": [...]}`` 422 shape.
    """
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        msg = first.get("msg", "Invalid request body")
        message = f"{loc}: {msg}" if loc else msg
    else:
        message = "Invalid request body"
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=_envelope("BAD_REQUEST", message),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all 500. Logs the traceback; the response body never leaks it."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope("INTERNAL_ERROR", "Internal server error"),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Wire every exception handler the migration spec requires (R9, R26, R28)."""
    app.add_exception_handler(HTTPException, _http_exception_handler)
    # Starlette raises its own HTTPException subclass for some paths (e.g.
    # unrouted requests). Register against it too so the envelope is
    # consistent across both surfaces.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


__all__ = [
    "ApiError",
    "install_exception_handlers",
]

# Silence lint: `Any` reserved for future typed exception payloads.
_ = Any
