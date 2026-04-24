"""Dependencies + middleware installers for the FastAPI app (M16 T57/T58).

Scope:
  * ``install_cors(app)`` — matches ``api_server.py::_cors_headers`` semantics
    (echo Origin when present + ``Allow-Credentials: true``; fall back to ``*``).
    Implemented via FastAPI's ``CORSMiddleware`` with ``allow_origin_regex=".*"``
    + ``allow_credentials=True`` because the strict combo ``allow_origins=["*"]``
    + ``allow_credentials=True`` is rejected by CORSMiddleware.
  * ``current_user`` — T58 real implementation. Bearer token first, session
    cookie fallback; raises 401 envelope via ``HTTPException``. Mirrors
    ``api_server.py::_authenticate`` — import-shares token-store helpers from
    ``scenecraft.vcs.auth`` so the two servers stay byte-for-byte compatible
    through the Phase A parallel-run window.
  * ``project_dir`` — resolves ``work_dir / name``, raises 404 envelope if
    the project directory doesn't exist.
  * ``PUBLIC_ROUTES`` — list of path patterns that skip ``current_user``.
    Consulted by ``routers/*`` when declaring endpoints; also used in the
    smoke check at app-boot time.

Spec R9, R11, R13–R17 (auth), R26–R28 (envelope), R48–R50 (CORS).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware


# ---------------------------------------------------------------------------
# CORS — byte-for-byte parity with legacy _cors_headers
# ---------------------------------------------------------------------------


_ALLOW_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Scenecraft-Branch"]


def install_cors(app: FastAPI) -> None:
    """Attach CORSMiddleware with legacy-equivalent configuration (R49, R50).

    Why ``allow_origin_regex`` instead of ``allow_origins=["*"]``:
      FastAPI's CORSMiddleware explicitly rejects ``allow_origins=["*"]`` when
      ``allow_credentials=True`` (the browser-side CORS spec forbids that
      combo). The regex form tells the middleware to echo whichever origin
      matches — for ``.*`` that's *every* origin — which is identical to the
      legacy server's "echo the request Origin" behavior, and the middleware
      emits ``Access-Control-Allow-Credentials: true`` only when the origin
      was echoed. That matches legacy exactly.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=_ALLOW_METHODS,
        allow_headers=_ALLOW_HEADERS,
    )


# ---------------------------------------------------------------------------
# Public routes — excluded from current_user gating
# ---------------------------------------------------------------------------


# Per spec R14. Kept here (not in each router) so adding a new router can't
# accidentally gate a login handshake endpoint.
PUBLIC_ROUTES: set[str] = {
    "/auth/login",
    "/auth/logout",  # POST is logout; clearing a cookie must not require auth.
    "/oauth/callback",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


# ---------------------------------------------------------------------------
# User model + auth dependency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    """Authenticated user.

    Mirrors the legacy server's ``_authenticated_user`` string identity plus
    the fingerprint+role claims that ``validate_token`` returns. A frozen
    dataclass is enough — we don't mutate user objects at request time.
    """

    id: str
    fingerprint: str = ""
    role: str = "editor"


def _raise_unauthorized() -> None:
    """Raise the 401 envelope legacy clients expect."""
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
    )


async def current_user(request: Request) -> User:
    """Authenticate the request via Authorization bearer or session cookie.

    Mirrors ``api_server.py::_authenticate``:
      1. If ``.scenecraft`` is absent, auth is disabled and a synthetic
         "local" user is returned. This matches the legacy "no .scenecraft =
         no auth" carve-out (line 126-127 of api_server.py) which keeps the
         dev loop working when a user runs ``scenecraft serve`` outside a
         project tree.
      2. Try ``Authorization: Bearer <token>`` first.
      3. Fall back to the ``scenecraft_jwt`` cookie.
      4. Validate via ``scenecraft.vcs.auth.validate_token``.
      5. Raise 401 on any failure — exception handler converts to the
         ``{"error": "UNAUTHORIZED", "message": ...}`` envelope.
    """
    from scenecraft.vcs.auth import (
        extract_bearer_token,
        extract_cookie_token,
        validate_token,
    )
    from scenecraft.vcs.bootstrap import find_root

    # Step 1 — detect the .scenecraft root. Use the app's work_dir as the
    # walk-start so ``find_root`` resolves the same way the legacy server
    # does (``make_handler`` calls ``find_root(work_dir)``).
    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    sc_root = find_root(work_dir) if work_dir is not None else find_root()

    if sc_root is None:
        # Auth disabled: return a canonical "local" user so downstream
        # handlers can still key per-user state.
        return User(id="local")

    # Step 2-3 — extract token.
    token = extract_bearer_token(request.headers.get("Authorization"))
    if not token:
        token = extract_cookie_token(request.headers.get("Cookie"))

    if not token:
        _raise_unauthorized()
        raise AssertionError("unreachable")  # for type-checker

    # Step 4-5 — validate.
    try:
        payload = validate_token(sc_root, token)
    except Exception:
        _raise_unauthorized()
        raise AssertionError("unreachable")

    return User(
        id=payload.get("sub", "unknown"),
        fingerprint=payload.get("fingerprint", ""),
        role=payload.get("role", "editor"),
    )


# ---------------------------------------------------------------------------
# Project dir dependency
# ---------------------------------------------------------------------------


def project_dir(
    name: str, request: Request, user: User = Depends(current_user)
) -> Path:
    """Resolve ``work_dir / name`` or raise 404 NOT_FOUND.

    Mirrors ``api_server.py::_require_project_dir``. The ``user`` dep is
    declared here so every route that accepts ``project_dir`` transitively
    gets auth — avoids routers forgetting to chain the two deps.
    """
    from scenecraft.api.errors import ApiError

    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    if work_dir is None:
        raise ApiError(
            "INTERNAL_ERROR",
            "File serving not configured (work_dir missing)",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    candidate = work_dir / name
    if not candidate.is_dir():
        raise ApiError(
            "NOT_FOUND",
            f"Project not found: {name}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return candidate


__all__ = [
    "PUBLIC_ROUTES",
    "User",
    "current_user",
    "install_cors",
    "project_dir",
]
