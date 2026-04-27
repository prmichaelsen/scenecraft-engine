"""Email/password authentication — PBKDF2 hashing + opaque session tokens.

Ported from the iris project's worker/auth.ts pattern. Uses Python stdlib
only: hashlib, os, secrets, base64.  No argon2-cffi or other native deps.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time

PBKDF2_ITERATIONS = 100_000
SALT_BYTES = 16
SESSION_TTL_DAYS = 30
SESSION_COOKIE = "scenecraft_session"


def hash_password(password: str) -> str:
    """Hash *password* with PBKDF2-SHA256.

    Returns ``"{iterations}${b64(salt)}${b64(hash)}"``.
    """
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against a stored hash produced by :func:`hash_password`."""
    parts = stored.split("$")
    if len(parts) != 3:
        return False
    try:
        iterations = int(parts[0])
        salt = base64.b64decode(parts[1])
        expected = base64.b64decode(parts[2])
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return secrets.compare_digest(actual, expected)


def new_session_token() -> str:
    """Generate a cryptographically random session token (base64url, no padding)."""
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def new_id() -> str:
    """Generate a short opaque ID (base64url, no padding)."""
    return base64.urlsafe_b64encode(os.urandom(12)).rstrip(b"=").decode()
