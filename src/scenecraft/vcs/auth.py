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
