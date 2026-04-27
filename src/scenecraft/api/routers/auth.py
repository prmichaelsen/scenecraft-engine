"""Auth router — login (one-time code → cookie) + logout (clear cookie).

Both routes are public (no ``current_user`` dep) — see spec R14 and
``deps.PUBLIC_ROUTES``. Mirrors legacy ``_handle_auth_login`` /
``_handle_auth_logout`` from ``api_server.py`` (lines 2475-2510).

Legacy parity:
  * Login uses 303 See Other + Location header + Set-Cookie with HttpOnly,
    SameSite=Lax, and Secure when the request scheme is HTTPS.
  * Logout returns ``{"ok": true}`` and a Max-Age=0 cookie.
  * When ``.scenecraft`` is absent, login 501s with ``AUTH_DISABLED``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from scenecraft.api.errors import ApiError

router = APIRouter(tags=["auth"])


def _sc_root_from_request(request: Request) -> Path | None:
    from scenecraft.vcs.bootstrap import find_root

    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    return find_root(work_dir) if work_dir is not None else find_root()


@router.get(
    "/auth/login",
    operation_id="auth_login",
    summary="Exchange a one-time login code for an HttpOnly session cookie",
    include_in_schema=True,
)
async def auth_login(
    request: Request, code: str | None = None, redirect_uri: str = "/"
) -> Response:
    from scenecraft.vcs.auth import build_cookie_header, consume_login_code

    sc_root = _sc_root_from_request(request)
    if sc_root is None:
        raise ApiError(
            "AUTH_DISABLED",
            "Auth is not enabled on this server",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )
    if not code:
        raise ApiError("BAD_REQUEST", "Missing code", status_code=status.HTTP_400_BAD_REQUEST)

    token = consume_login_code(sc_root, code)
    if not token:
        raise ApiError(
            "INVALID_CODE",
            "Login code is invalid, expired, or already used",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    secure = request.url.scheme == "https"
    cookie = build_cookie_header(token, secure=secure)

    # Legacy used 303 See Other. Starlette's RedirectResponse defaults to 307
    # (preserve method), so we explicitly pass 303 — matches legacy byte-for-byte.
    resp = RedirectResponse(url=redirect_uri or "/", status_code=303)
    # RedirectResponse manages Location; set the cookie via raw header so we
    # can reuse ``build_cookie_header`` exactly (and emit the legacy string).
    resp.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return resp


@router.post(
    "/auth/logout",
    operation_id="auth_logout",
    summary="Clear the session cookie",
    include_in_schema=True,
)
async def auth_logout() -> Response:
    from scenecraft.vcs.auth import build_clear_cookie_header

    resp = JSONResponse(content={"ok": True})
    resp.raw_headers.append((b"set-cookie", build_clear_cookie_header().encode("latin-1")))
    return resp


__all__ = ["router"]
