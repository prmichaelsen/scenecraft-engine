"""JWT-based authentication for scenecraft VCS."""

from __future__ import annotations

import getpass
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import jwt

from .bootstrap import get_server_db, find_root

TOKEN_ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24
LOGIN_CODE_TTL_SECONDS = 300  # 5 minutes

COOKIE_NAME = "scenecraft_jwt"


# ── Login code storage (one-time codes exchanged for cookie) ─────

_LOGIN_CODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_codes (
    code TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


def _ensure_login_codes_table(sc_root: Path) -> sqlite3.Connection:
    conn = get_server_db(sc_root)
    conn.executescript(_LOGIN_CODES_SCHEMA)
    conn.commit()
    return conn


def create_login_code(sc_root: Path, token: str) -> str:
    """Store a JWT against a short-lived one-time login code. Returns the code."""
    code = secrets.token_urlsafe(24)
    expires_at = int((datetime.now(tz=timezone.utc) + timedelta(seconds=LOGIN_CODE_TTL_SECONDS)).timestamp())
    conn = _ensure_login_codes_table(sc_root)
    # Garbage-collect expired codes on every insert
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now_ts,))
    conn.execute("INSERT INTO login_codes (code, token, expires_at) VALUES (?, ?, ?)", (code, token, expires_at))
    conn.commit()
    conn.close()
    return code


def consume_login_code(sc_root: Path, code: str) -> str | None:
    """Exchange a login code for the JWT. Single-use — deletes on consumption.

    Returns None if code is invalid, expired, or already used.
    """
    conn = _ensure_login_codes_table(sc_root)
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    row = conn.execute(
        "SELECT token, expires_at FROM login_codes WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute("DELETE FROM login_codes WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    if row["expires_at"] < now_ts:
        return None
    return row["token"]


def _get_secret(sc_root: Path) -> str:
    """Load or create the server signing secret."""
    secret_path = sc_root / "secret.key"
    if secret_path.exists():
        return secret_path.read_text().strip()
    secret = secrets.token_hex(32)
    secret_path.write_text(secret)
    secret_path.chmod(0o600)
    return secret


def generate_token(sc_root: Path, username: str | None = None, expiry_hours: int = TOKEN_EXPIRY_HOURS) -> str:
    """Generate a JWT for the given user (or current OS user).

    Returns the encoded JWT string.
    Raises ValueError if user is not registered.
    """
    username = username or getpass.getuser()
    secret = _get_secret(sc_root)

    conn = get_server_db(sc_root)
    row = conn.execute("SELECT username, pubkey_fingerprint, role FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if row is None:
        raise ValueError(f"User '{username}' is not registered. Ask an admin to run: scenecraft vcs user add {username}")

    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": row["username"],
        "fingerprint": row["pubkey_fingerprint"],
        "role": row["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=expiry_hours)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=TOKEN_ALGORITHM)


def validate_token(sc_root: Path, token: str) -> dict:
    """Validate a JWT and return the decoded payload.

    Returns dict with keys: sub, fingerprint, role, iat, exp.
    Raises jwt.ExpiredSignatureError if expired.
    Raises jwt.InvalidTokenError for any other validation failure.
    """
    secret = _get_secret(sc_root)
    return jwt.decode(token, secret, algorithms=[TOKEN_ALGORITHM])


def get_username_from_token(sc_root: Path, token: str) -> str | None:
    """Extract username from a valid token, or return None if invalid."""
    try:
        payload = validate_token(sc_root, token)
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extract the token from an 'Authorization: Bearer <token>' header."""
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def extract_cookie_token(cookie_header: str | None) -> str | None:
    """Extract the scenecraft_jwt token from a Cookie header."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip() == COOKIE_NAME:
                return value.strip()
    return None


def build_cookie_header(token: str, max_age_seconds: int = TOKEN_EXPIRY_HOURS * 3600, secure: bool = False) -> str:
    """Build a Set-Cookie header value for the JWT.

    Uses HttpOnly + SameSite=Lax. Path=/. Secure flag optional (enable behind HTTPS).
    """
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age_seconds}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def build_clear_cookie_header() -> str:
    """Build a Set-Cookie header that clears the auth cookie."""
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
