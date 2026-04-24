"""Tests for the double-gate paid-plugin auth middleware.

Each test sets up a minimal .scenecraft with users, orgs, and API keys, then
simulates HTTP request headers against the middleware decorator to verify the
six auth gates.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from scenecraft.vcs.bootstrap import (
    init_root,
    create_user,
    add_user_to_org,
    create_org,
    get_server_db,
)
from scenecraft.vcs.auth import generate_token
from scenecraft.auth_middleware import (
    require_paid_plugin_auth,
    hash_api_key,
    PaidPluginAuthContext,
    PBKDF2_ITERATIONS,
)


# ── Helpers ──────────────────────────────────────────────────────


def _issue_key(sc_root: Path, username: str, expires_delta_days: int = 30, revoked: bool = False) -> str:
    """Issue an API key and return the raw key string."""
    import uuid

    raw_key = secrets.token_urlsafe(32)
    salt = os.urandom(16)
    key_hash = hash_api_key(raw_key, salt)
    key_id = f"ak_{uuid.uuid4().hex[:12]}"
    now = datetime.now(tz=timezone.utc)
    expires_at = (now + timedelta(days=expires_delta_days)).isoformat()
    revoked_at = now.isoformat() if revoked else None

    conn = get_server_db(sc_root)
    conn.execute(
        "INSERT INTO api_keys (id, username, key_hash, salt, issued_by, issued_at, expires_at, revoked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (key_id, username, key_hash, salt.hex(), "admin", now.isoformat(), expires_at, revoked_at),
    )
    conn.commit()
    conn.close()
    return raw_key


def _issue_key_with_expiry(sc_root: Path, username: str, expires_at_iso: str) -> str:
    """Issue an API key with an explicit expires_at (for testing expired keys)."""
    import uuid

    raw_key = secrets.token_urlsafe(32)
    salt = os.urandom(16)
    key_hash = hash_api_key(raw_key, salt)
    key_id = f"ak_{uuid.uuid4().hex[:12]}"
    now = datetime.now(tz=timezone.utc).isoformat()

    conn = get_server_db(sc_root)
    conn.execute(
        "INSERT INTO api_keys (id, username, key_hash, salt, issued_by, issued_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (key_id, username, key_hash, salt.hex(), "admin", now, expires_at_iso),
    )
    conn.commit()
    conn.close()
    return raw_key


class FakeHandler:
    """Minimal stand-in for SceneCraftHandler — provides .headers and ._error / ._json_response."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.error_calls: list[tuple[int, str, str]] = []
        self._paid_auth_ctx: PaidPluginAuthContext | None = None

    def _error(self, status: int, code: str, message: str):
        self.error_calls.append((status, code, message))

    def _json_response(self, obj, status: int = 200):
        self._response = (status, obj)


def _make_handler_with_headers(
    sc_root: Path,
    *,
    token: str | None = None,
    api_key: str | None = None,
    org: str | None = None,
) -> FakeHandler:
    """Build a FakeHandler with the given auth headers pre-populated."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["X-Scenecraft-API-Key"] = api_key
    if org:
        headers["X-Scenecraft-Org"] = org
    return FakeHandler(headers)


def _call_protected(sc_root: Path, handler: FakeHandler) -> bool:
    """Invoke a no-op protected handler and return True if the inner function ran."""
    ran = False

    @require_paid_plugin_auth(sc_root)
    def _protected(self):
        nonlocal ran
        ran = True

    _protected(handler)
    return ran


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def sc_root(tmp_path):
    """Initialise a .scenecraft with org 'acme' and admin user 'alice'."""
    init_root(tmp_path, org_name="acme", admin_username="alice")
    sc = tmp_path / ".scenecraft"
    # Clear must_change_password for alice (admin created via init_root doesn't go through create_user)
    conn = get_server_db(sc)
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'alice'")
    conn.commit()
    conn.close()
    return sc


@pytest.fixture
def alice_token(sc_root):
    return generate_token(sc_root, username="alice")


@pytest.fixture
def alice_key(sc_root):
    return _issue_key(sc_root, "alice")


# ── Tests ────────────────────────────────────────────────────────


def test_rejects_missing_session(sc_root):
    """Gate 1: no JWT at all -> 401."""
    handler = _make_handler_with_headers(sc_root, api_key="some-key")
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 401
    assert "session" in handler.error_calls[0][2].lower() or "Missing" in handler.error_calls[0][2]


def test_rejects_missing_api_key_header(sc_root, alice_token):
    """Gate 2: valid JWT but no X-Scenecraft-API-Key -> 401."""
    handler = _make_handler_with_headers(sc_root, token=alice_token)
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 401
    assert "API-Key" in handler.error_calls[0][2] or "api" in handler.error_calls[0][2].lower()


def test_rejects_session_key_mismatch(sc_root, alice_token):
    """Gate 3: valid JWT + wrong API key -> 401."""
    # Issue a key for alice, but present a garbage key value
    _issue_key(sc_root, "alice")
    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key="not-a-real-key")
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 401


def test_rejects_expired_api_key(sc_root, alice_token):
    """Gate 3: valid JWT + expired API key -> 401."""
    past = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
    raw_key = _issue_key_with_expiry(sc_root, "alice", past)
    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key=raw_key)
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 401


def test_rejects_revoked_api_key(sc_root, alice_token):
    """Gate 3: valid JWT + revoked API key -> 401."""
    raw_key = _issue_key(sc_root, "alice", revoked=True)
    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key=raw_key)
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 401


def test_forces_password_change_on_first_login(sc_root):
    """Gate 4: must_change_password = 1 -> 403."""
    # Create a new user with must_change_password=1 (default from create_user)
    create_user(sc_root.parent, "bob", role="editor")
    add_user_to_org(sc_root, "acme", "bob")
    raw_key = _issue_key(sc_root, "bob")
    token = generate_token(sc_root, username="bob")

    handler = _make_handler_with_headers(sc_root, token=token, api_key=raw_key, org="acme")
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 403
    assert "password" in handler.error_calls[0][2].lower()


def test_active_org_from_header(sc_root, alice_token, alice_key):
    """Gate 5: org specified via X-Scenecraft-Org -> 200, context.org matches."""
    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key=alice_key, org="acme")
    assert _call_protected(sc_root, handler)
    assert handler._paid_auth_ctx is not None
    assert handler._paid_auth_ctx.org == "acme"
    assert handler._paid_auth_ctx.username == "alice"


def test_active_org_from_session_fallback(sc_root, alice_key):
    """Gate 5: no org header, but JWT has last_active_org -> use that."""
    import jwt as pyjwt
    from scenecraft.vcs.auth import _get_secret, TOKEN_ALGORITHM

    secret = _get_secret(sc_root)
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": "alice",
        "fingerprint": "",
        "role": "admin",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "last_active_org": "acme",
    }
    token = pyjwt.encode(payload, secret, algorithm=TOKEN_ALGORITHM)

    handler = _make_handler_with_headers(sc_root, token=token, api_key=alice_key)
    assert _call_protected(sc_root, handler)
    assert handler._paid_auth_ctx.org == "acme"


def test_ambiguous_org_rejected(sc_root, alice_token, alice_key):
    """Gate 5: user in multiple orgs, no header, no session hint -> 400."""
    # Add alice to a second org
    create_org(sc_root, "other-org")
    add_user_to_org(sc_root, "other-org", "alice")

    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key=alice_key)
    assert not _call_protected(sc_root, handler)
    assert handler.error_calls[0][0] == 400
    assert "org" in handler.error_calls[0][2].lower()


def test_single_org_user_resolved_automatically(sc_root, alice_token, alice_key):
    """Gate 5: user in exactly one org, no header -> auto-resolve."""
    handler = _make_handler_with_headers(sc_root, token=alice_token, api_key=alice_key)
    assert _call_protected(sc_root, handler)
    assert handler._paid_auth_ctx.org == "acme"
    assert handler._paid_auth_ctx.username == "alice"
    assert handler._paid_auth_ctx.api_key_id.startswith("ak_")
