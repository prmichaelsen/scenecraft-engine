"""Auth router — login (one-time code → cookie) + email/password login + logout + me.

Both legacy code-based login and new email/password login are public (no
``current_user`` dep) — see spec R14 and ``deps.PUBLIC_ROUTES``.

New endpoints (email/password auth):
  * POST /api/auth/login  — email+password body, sets session cookie
  * POST /api/auth/logout — clears session cookie, deletes DB session
  * GET  /api/auth/me     — returns current user from session cookie
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from scenecraft.api.errors import ApiError

router = APIRouter(tags=["auth"])


def _sc_root_from_request(request: Request) -> Path | None:
    from scenecraft.vcs.bootstrap import find_root

    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    return find_root(work_dir) if work_dir is not None else find_root()


# ── Legacy code-based login (GET /auth/login?code=...) ─────────────

@router.get(
    "/auth/login",
    operation_id="auth_login_code",
    summary="Exchange a one-time login code for an HttpOnly session cookie",
    include_in_schema=True,
)
async def auth_login_code(
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

    resp = RedirectResponse(url=redirect_uri or "/", status_code=303)
    resp.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    return resp


# ── Email/password login (POST /api/auth/login) ───────────────────


class LoginBody(BaseModel):
    email: str
    password: str


@router.post(
    "/api/auth/login",
    operation_id="auth_email_login",
    summary="Authenticate with email and password, receive a session cookie",
    include_in_schema=True,
)
async def auth_email_login(request: Request, body: LoginBody) -> Response:
    from scenecraft.auth import (
        SESSION_COOKIE,
        SESSION_TTL_DAYS,
        new_session_token,
        verify_password,
    )
    from scenecraft.vcs.bootstrap import get_server_db

    sc_root = _sc_root_from_request(request)
    if sc_root is None:
        raise ApiError(
            "AUTH_DISABLED",
            "Auth is not enabled on this server",
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
        )

    conn = get_server_db(sc_root)
    email = body.email.strip().lower()

    row = conn.execute(
        "SELECT username, email, password_hash, role, disabled FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    # Same error for wrong email AND wrong password — no user enumeration.
    invalid_msg = "Invalid email or password"

    if not row or not row["password_hash"]:
        conn.close()
        raise ApiError("UNAUTHORIZED", invalid_msg, status_code=status.HTTP_401_UNAUTHORIZED)

    if row["disabled"]:
        conn.close()
        raise ApiError("UNAUTHORIZED", invalid_msg, status_code=status.HTTP_401_UNAUTHORIZED)

    if not verify_password(body.password, row["password_hash"]):
        conn.close()
        raise ApiError("UNAUTHORIZED", invalid_msg, status_code=status.HTTP_401_UNAUTHORIZED)

    # Create session.
    token = new_session_token()
    now = int(time.time())
    expires = now + SESSION_TTL_DAYS * 86400

    conn.execute(
        "INSERT INTO auth_sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, row["username"], now, expires),
    )
    conn.commit()
    conn.close()

    resp = JSONResponse(
        content={"user": {"id": row["username"], "email": row["email"], "role": row["role"]}},
    )
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


# ── Logout (POST /api/auth/logout) ────────────────────────────────

@router.post(
    "/api/auth/logout",
    operation_id="auth_email_logout",
    summary="Clear the session cookie and delete the server-side session",
    include_in_schema=True,
)
async def auth_email_logout(request: Request) -> Response:
    from scenecraft.auth import SESSION_COOKIE
    from scenecraft.vcs.bootstrap import get_server_db

    token = _extract_session_cookie(request)

    if token:
        sc_root = _sc_root_from_request(request)
        if sc_root is not None:
            conn = get_server_db(sc_root)
            conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            conn.commit()
            conn.close()

    resp = JSONResponse(content={"ok": True})
    resp.set_cookie(
        SESSION_COOKIE,
        "",
        max_age=0,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


# ── Legacy logout (POST /auth/logout) ─────────────────────────────

@router.post(
    "/auth/logout",
    operation_id="auth_logout_legacy",
    summary="Clear the legacy JWT session cookie",
    include_in_schema=True,
)
async def auth_logout_legacy() -> Response:
    from scenecraft.vcs.auth import build_clear_cookie_header

    resp = JSONResponse(content={"ok": True})
    resp.raw_headers.append((b"set-cookie", build_clear_cookie_header().encode("latin-1")))
    return resp


# ── Me (GET /api/auth/me) ─────────────────────────────────────────

@router.get(
    "/api/auth/me",
    operation_id="auth_me",
    summary="Return the currently authenticated user from session cookie",
    include_in_schema=True,
)
async def auth_me(request: Request) -> Response:
    from scenecraft.auth import SESSION_COOKIE
    from scenecraft.vcs.bootstrap import get_server_db

    token = _extract_session_cookie(request)
    if not token:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)

    sc_root = _sc_root_from_request(request)
    if sc_root is None:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)

    conn = get_server_db(sc_root)
    row = conn.execute(
        "SELECT u.username, u.email, u.role, s.expires_at "
        "FROM auth_sessions s JOIN users u ON u.username = s.user_id "
        "WHERE s.token = ?",
        (token,),
    ).fetchone()
    conn.close()

    if not row:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=status.HTTP_401_UNAUTHORIZED)

    if row["expires_at"] < int(time.time()):
        raise ApiError("UNAUTHORIZED", "Session expired", status_code=status.HTTP_401_UNAUTHORIZED)

    return JSONResponse(content={
        "id": row["username"],
        "email": row["email"],
        "role": row["role"],
    })


# ── Helpers ────────────────────────────────────────────────────────

def _extract_session_cookie(request: Request) -> str | None:
    """Extract the scenecraft_session cookie value from the request."""
    from scenecraft.auth import SESSION_COOKIE

    cookie_header = request.headers.get("cookie")
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip() == SESSION_COOKIE:
                return value.strip()
    return None


__all__ = ["router"]
