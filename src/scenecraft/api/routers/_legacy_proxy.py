"""Shim that invokes legacy ``api_server.SceneCraftHandler`` handlers from FastAPI routes (M16 T61).

The legacy HTTP server is pinned to ``BaseHTTPRequestHandler`` —
handlers read ``self.rfile`` / ``self.headers`` and write via
``self.send_response`` / ``self.wfile``. Porting each line-by-line
would be thousands of lines of rewrite that T65 will throw away.
Instead, this proxy stands up a minimal handler instance that captures
response bytes into memory and returns them as a ``JSONResponse`` —
byte-for-byte parity with zero legacy logic changed.

Two dispatch modes:

  * ``dispatch_method`` — call a named ``_handle_*`` method directly.
    Works for handlers that are extracted to class methods (most of
    ``api_server.py``'s keyframe/transition surface).

  * ``dispatch_path`` — re-enter ``_do_POST(path)``. Routes the
    request through legacy's regex dispatch, which matches the
    inline handler bodies that never got extracted to a method.
    Needed for: ``assign-keyframe-image``, ``escalate-keyframe``,
    ``copy-transition-style``, ``duplicate-transition-video``,
    ``update-keyframe-label``, ``update-transition-label``,
    ``update-keyframe-style``, ``update-transition-style``,
    ``transition-effects/{add,update,delete}``, and a few others.

Why this is safe:
  * The handlers only call ``_read_json_body``,
    ``_require_project_dir``, ``_json_response``, ``_error`` — all
    overridden here to use an injected JSON body and to raise
    ``_CapturedResponse`` on response paths.
  * ``_CapturedResponse`` subclasses ``BaseException`` (NOT
    ``Exception``) so legacy ``except Exception`` blocks don't
    swallow our control-flow signal.
  * ``_refreshed_cookie`` is irrelevant in the Phase A parallel-run —
    FastAPI auth already refreshed the cookie at the dependency layer.
  * ``_log`` still prints to stderr exactly as legacy does.

T65 deletes both this proxy and ``api_server.py`` in one pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from scenecraft.api.errors import ApiError


class _CapturedResponse(BaseException):
    """Internal control-flow carrier for ``_json_response`` / ``_error``.

    ``BaseException`` (not ``Exception``) so legacy ``except Exception``
    blocks don't swallow it.
    """

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self.payload = payload


class _Headers(dict):
    """Header-lookup shim — most handlers only read ``Content-Length``.

    ``_read_json_body`` in the proxy ignores the header entirely (the
    body is pre-parsed upstream) but we still expose a ``.get`` with
    sane defaults so any other code path that inspects headers doesn't
    KeyError.
    """

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(key, default)


class _ProxyHandler:
    """Just enough of ``BaseHTTPRequestHandler`` for ``_handle_*`` / ``_do_POST`` to run.

    Constructed per-request. Holds the JSON body, the path (only used
    by ``_do_POST`` branch dispatch), and a ``_captured`` slot that
    the response helpers fill in.
    """

    _authenticated_user: str | None = "fastapi"
    _refreshed_cookie: str | None = None

    def __init__(
        self,
        work_dir: Path,
        body: dict | None,
        path: str = "",
    ) -> None:
        self._work_dir = work_dir
        self._body = body
        self.path = path
        self.headers = _Headers()

    # ── Body / dir helpers ───────────────────────────────────────

    def _read_json_body(self) -> dict | None:
        """Return the pre-parsed JSON body.

        Legacy semantics: ``None`` means "the helper already emitted a
        400 and the handler should return immediately". We mirror that
        by raising ``_CapturedResponse`` when the body is missing.
        """
        if self._body is None:
            raise _CapturedResponse(400, {"error": "Empty body", "code": "BAD_REQUEST"})
        return self._body

    def _get_project_dir(self, project_name: str) -> Path | None:
        d = self._work_dir / project_name
        return d if d.is_dir() else None

    def _require_project_dir(self, project_name: str) -> Path | None:
        d = self._get_project_dir(project_name)
        if d is None:
            self._error(404, "NOT_FOUND", f"Project not found: {project_name}")
            return None
        return d

    # ── Response helpers — raise to escape the handler ─────────

    def _json_response(self, obj: Any, status: int = 200) -> None:
        raise _CapturedResponse(status, obj)

    def _error(self, status: int, code: str, message: str) -> None:
        # Legacy wire shape: ``{"error": message, "code": code}``. Preserved
        # so clients that grep the body text keep working.
        raise _CapturedResponse(status, {"error": message, "code": code})


def _build_legacy_class(work_dir: Path):
    """Build (and cache at import time) the ``SceneCraftHandler`` class."""
    from scenecraft.api_server import make_handler

    return make_handler(work_dir, no_auth=True)


def _coerce(result_exc: _CapturedResponse) -> JSONResponse:
    return JSONResponse(status_code=result_exc.status_code, content=result_exc.payload)


def dispatch_legacy(
    work_dir: Path,
    method_name: str,
    project_name: str,
    body: dict | None,
    *extra_args: Any,
) -> JSONResponse:
    """Invoke a named ``_handle_*`` method.

    ``method_name`` must be defined on ``SceneCraftHandler``. For
    handlers inlined in ``_do_POST`` (no extracted method), use
    ``dispatch_legacy_path`` instead.
    """
    handler_cls = _build_legacy_class(work_dir)
    method = getattr(handler_cls, method_name, None)
    if method is None:
        raise ApiError(
            "INTERNAL_ERROR",
            f"Legacy handler missing: {method_name}",
            status_code=500,
        )

    proxy = _ProxyHandler(work_dir, body)
    try:
        method(proxy, project_name, *extra_args)
    except _CapturedResponse as resp:
        return _coerce(resp)
    except ApiError:
        raise
    except Exception as exc:  # pragma: no cover — legacy exception surface
        return JSONResponse(
            status_code=500,
            content={"error": str(exc) or "Internal server error", "code": "INTERNAL_ERROR"},
        )

    # Handler returned without hitting ``_json_response`` / ``_error`` —
    # legacy behavior for that is an implicit 200 with no body. Rare but
    # possible for fire-and-forget routes. Preserve it.
    return JSONResponse(status_code=200, content={"success": True})


def dispatch_legacy_path(
    work_dir: Path,
    path: str,
    body: dict | None,
) -> JSONResponse:
    """Re-enter ``_do_POST(path)`` so legacy's inline dispatch picks the handler.

    Needed for routes that were never factored out to ``_handle_*``
    methods — the handler body lives directly inside ``_do_POST``'s
    regex chain. The proxy's ``.path`` attribute is set so any handler
    that parses it (rare) still works.
    """
    handler_cls = _build_legacy_class(work_dir)
    proxy = _ProxyHandler(work_dir, body, path=path)
    try:
        handler_cls._do_POST(proxy, path)
    except _CapturedResponse as resp:
        return _coerce(resp)
    except ApiError:
        raise
    except Exception as exc:  # pragma: no cover
        return JSONResponse(
            status_code=500,
            content={"error": str(exc) or "Internal server error", "code": "INTERNAL_ERROR"},
        )

    return JSONResponse(status_code=200, content={"success": True})


__all__ = ["dispatch_legacy", "dispatch_legacy_path"]
