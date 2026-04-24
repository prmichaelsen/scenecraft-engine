"""Error-handling plumbing for the FastAPI app (M16 T57/T58, spec R9, R26-R28).

Canonical envelope (R9): ``{"error": "<CODE>", "message": "<human text>"}``.

Handlers installed by ``install_exception_handlers``:
  * ``HTTPException`` / Starlette ``HTTPException`` → canonical envelope with
    code derived from status (or ``ApiError.code`` override).
  * ``RequestValidationError`` → ``400 BAD_REQUEST`` (not FastAPI's default
    422); first error surfaced; missing-field messages match legacy text
    (``"Missing '<field>'"``).
  * Unrouted GET/POST/... (Starlette's internal 404) → ``NOT_FOUND`` envelope
    with ``"No route: <METHOD> <PATH>"`` message.
  * Bare ``Exception`` → ``500 INTERNAL_ERROR``; traceback logged at ERROR
    level but never leaked to the response body. The message is ``str(exc)``
    so clients can still see the failure mode without seeing file paths or
    line numbers.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, status
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
    to bypass the default status→code mapping — useful for domain codes like
    ``PLUGIN_ERROR`` that don't correspond to an HTTP status 1:1.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def _envelope(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Canonical envelope for any ``HTTPException``.

    Unrouted paths: Starlette raises a plain ``HTTPException(404, "Not Found")``
    from ``ExceptionMiddleware`` before any route matches. We detect that case
    by checking ``request.scope.get("route")`` — if no route was matched, the
    404 message is replaced with ``"No route: <METHOD> <PATH>"`` so clients
    can tell "unknown endpoint" from "endpoint found, resource missing".
    """
    status_code = exc.status_code
    code = getattr(exc, "code", None) or _code_for_status(status_code)

    # Detect the unknown-route case: no route was matched in the scope.
    if status_code == status.HTTP_404_NOT_FOUND and request.scope.get("route") is None:
        message = f"No route: {request.method} {request.url.path}"
    else:
        # HTTPException.detail can be a string or a dict; normalize to text.
        if isinstance(exc.detail, str):
            message = exc.detail
        elif exc.detail is None:
            message = ""
        else:
            message = str(exc.detail)

    headers = getattr(exc, "headers", None) or None
    return JSONResponse(
        status_code=status_code,
        content=_envelope(code, message),
        headers=headers,
    )


def _format_validation_message(err: dict) -> str:
    """Translate a single Pydantic error dict to legacy envelope text.

    Legacy behavior:
      * Missing-field errors become ``"Missing '<field>'"`` so front-ends that
        already parse that message (e.g., the keyframe editor) keep working.
      * Everything else becomes ``"<loc>: <msg>"`` with ``body.`` stripped
        from the location so the client sees ``"start_time: ..."`` rather
        than ``"body.start_time: ..."``.
    """
    err_type = err.get("type", "")
    raw_loc = err.get("loc", ())
    parts = [str(p) for p in raw_loc if p != "body"]

    if err_type in ("missing", "value_error.missing") or "missing" in err_type:
        field_name = parts[-1] if parts else "field"
        return f"Missing '{field_name}'"

    loc = ".".join(parts)
    msg = err.get("msg", "Invalid request body")
    return f"{loc}: {msg}" if loc else msg


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten Pydantic validation errors to 400 BAD_REQUEST envelope (R26).

    We always emit 400, never 422, so existing clients don't need to learn a
    new error format.
    """
    errors = exc.errors()
    if errors:
        message = _format_validation_message(errors[0])
    else:
        message = "Invalid request body"
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=_envelope("BAD_REQUEST", message),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all 500 (R28).

    The traceback is logged at ERROR level (``logger.exception`` attaches
    ``exc_info``). The response body carries ``str(exc)`` only — never the
    traceback — so we don't leak file paths or stack contents to clients.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope("INTERNAL_ERROR", str(exc) or "Internal server error"),
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Wire every exception handler the migration spec requires (R9, R26, R28)."""
    app.add_exception_handler(HTTPException, _http_exception_handler)
    # Starlette raises its own HTTPException subclass for unrouted requests.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)


__all__ = [
    "ApiError",
    "install_exception_handlers",
]
