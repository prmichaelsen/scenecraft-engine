"""Double-gate auth middleware for paid-plugin endpoints.

Gate 1: JWT session (same as existing _authenticate in api_server).
Gate 2: X-Scenecraft-API-Key header — hashed with PBKDF2 and looked up in api_keys.

Only applied to paid-plugin routes, NOT all endpoints.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ── Key hashing ──────────────────────────────────────────────────

PBKDF2_ITERATIONS = 600_000


def hash_api_key(raw_key: str, salt: bytes) -> str:
    """Derive a hex-encoded PBKDF2-HMAC-SHA256 hash from a raw API key + salt."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        raw_key.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    ).hex()


# ── Request context attached after successful auth ───────────────

@dataclass
class PaidPluginAuthContext:
    """Attached to the request handler after successful double-gate auth."""
    username: str
    org: str
    api_key_id: str


# ── Middleware ────────────────────────────────────────────────────

def require_paid_plugin_auth(sc_root: Path):
    """Return a decorator that enforces double-gate auth on a handler method.

    Usage inside make_handler()::

        @require_paid_plugin_auth(sc_root)
        def _handle_paid_endpoint(self):
            ctx = self._paid_auth_ctx  # PaidPluginAuthContext
            ...

    The decorator is designed to wrap ``SceneCraftHandler`` instance methods
    (``self`` is the first positional argument). It writes auth errors directly
    on the handler and returns ``None`` to signal the caller to stop processing.

    Implementation gates (in order):
      1. Decode JWT session -> resolve username.  Reject 401 if absent/invalid.
      2. Read X-Scenecraft-API-Key header.  Reject 401 if absent.
      3. Hash key, look up in api_keys WHERE username matches AND not revoked AND
         not expired.  Reject 401 if not found.
      4. If users.must_change_password = 1 -> reject 403.
      5. Resolve active org (header > session fallback > single-org shortcut).
         Reject 400 if ambiguous.
      6. Attach PaidPluginAuthContext to handler as ``_paid_auth_ctx``.
    """

    def decorator(handler_func: Callable):
        def wrapper(handler_self, *args, **kwargs):
            from scenecraft.vcs.auth import (
                extract_bearer_token,
                extract_cookie_token,
                validate_token,
            )
            from scenecraft.vcs.bootstrap import get_server_db

            # ── Gate 1: JWT session ─────────────────────────────
            token = extract_bearer_token(
                handler_self.headers.get("Authorization")
            )
            if not token:
                token = extract_cookie_token(
                    handler_self.headers.get("Cookie")
                )
            if not token:
                handler_self._error(401, "UNAUTHORIZED", "Missing session token")
                return None

            try:
                payload = validate_token(sc_root, token)
            except Exception:
                handler_self._error(401, "UNAUTHORIZED", "Invalid or expired session token")
                return None

            username = payload.get("sub")
            if not username:
                handler_self._error(401, "UNAUTHORIZED", "Malformed session token")
                return None

            # ── Gate 2: API key header ──────────────────────────
            raw_key = handler_self.headers.get("X-Scenecraft-API-Key")
            if not raw_key:
                handler_self._error(401, "UNAUTHORIZED", "Missing X-Scenecraft-API-Key header")
                return None

            conn = get_server_db(sc_root)
            now_iso = datetime.now(tz=timezone.utc).isoformat()

            # Look up all non-revoked, non-expired keys for this user and try
            # each salt. api_keys count per user is expected to be very small
            # (single digits), so iterating is fine.
            rows = conn.execute(
                "SELECT id, key_hash, salt FROM api_keys "
                "WHERE username = ? AND revoked_at IS NULL AND expires_at > ?",
                (username, now_iso),
            ).fetchall()

            matched_key_id = None
            for row in rows:
                candidate_hash = hash_api_key(raw_key, bytes.fromhex(row["salt"]))
                if candidate_hash == row["key_hash"]:
                    matched_key_id = row["id"]
                    break

            if matched_key_id is None:
                handler_self._error(401, "UNAUTHORIZED", "Invalid API key or session/key user mismatch")
                return None

            # ── Gate 3: must_change_password ─────────────────────
            user_row = conn.execute(
                "SELECT must_change_password FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if user_row and user_row["must_change_password"]:
                handler_self._error(
                    403,
                    "PASSWORD_CHANGE_REQUIRED",
                    "Password change required before using paid-plugin endpoints",
                )
                return None

            # ── Gate 4: resolve active org ───────────────────────
            org_header = handler_self.headers.get("X-Scenecraft-Org")
            resolved_org = None

            if org_header:
                # Verify user is actually a member of this org
                membership = conn.execute(
                    "SELECT 1 FROM org_members WHERE org = ? AND username = ?",
                    (org_header, username),
                ).fetchone()
                if membership:
                    resolved_org = org_header
                else:
                    handler_self._error(
                        400,
                        "ORG_NOT_FOUND",
                        f"User '{username}' is not a member of org '{org_header}'",
                    )
                    return None
            else:
                # Fallback: session last_active_org (stored in JWT if present)
                last_org = payload.get("last_active_org")
                if last_org:
                    membership = conn.execute(
                        "SELECT 1 FROM org_members WHERE org = ? AND username = ?",
                        (last_org, username),
                    ).fetchone()
                    if membership:
                        resolved_org = last_org

                if resolved_org is None:
                    # Final fallback: user belongs to exactly one org
                    orgs = conn.execute(
                        "SELECT org FROM org_members WHERE username = ?",
                        (username,),
                    ).fetchall()
                    if len(orgs) == 1:
                        resolved_org = orgs[0]["org"]
                    else:
                        handler_self._error(
                            400,
                            "AMBIGUOUS_ORG",
                            "User belongs to multiple orgs; specify via X-Scenecraft-Org header",
                        )
                        return None

            # ── Attach context and proceed ───────────────────────
            handler_self._paid_auth_ctx = PaidPluginAuthContext(
                username=username,
                org=resolved_org,
                api_key_id=matched_key_id,
            )
            return handler_func(handler_self, *args, **kwargs)

        # Preserve the original function name for debugging / routing introspection
        wrapper.__name__ = handler_func.__name__
        wrapper.__doc__ = handler_func.__doc__
        return wrapper

    return decorator
