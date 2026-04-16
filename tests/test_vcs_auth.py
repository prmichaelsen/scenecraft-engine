"""Tests for scenecraft VCS auth — JWT token generation and validation."""

import time

import jwt as pyjwt
import pytest

from scenecraft.vcs.bootstrap import init_root, create_user
from scenecraft.vcs.auth import (
    generate_token,
    validate_token,
    get_username_from_token,
    extract_bearer_token,
    _get_secret,
)


@pytest.fixture
def sc_root(tmp_path):
    init_root(tmp_path, org_name="test-org", admin_username="alice")
    return tmp_path / ".scenecraft"


def test_secret_created_on_first_use(sc_root):
    secret = _get_secret(sc_root)
    assert len(secret) == 64  # 32 bytes hex
    # Second call returns same secret
    assert _get_secret(sc_root) == secret


def test_generate_token_for_registered_user(sc_root):
    token = generate_token(sc_root, username="alice")
    assert isinstance(token, str)
    assert len(token) > 20


def test_generate_token_unregistered_user_raises(sc_root):
    with pytest.raises(ValueError, match="not registered"):
        generate_token(sc_root, username="nobody")


def test_validate_token_roundtrip(sc_root):
    token = generate_token(sc_root, username="alice")
    payload = validate_token(sc_root, token)
    assert payload["sub"] == "alice"
    assert payload["role"] == "admin"
    assert "iat" in payload
    assert "exp" in payload


def test_expired_token_rejected(sc_root):
    token = generate_token(sc_root, username="alice", expiry_hours=0)
    # Token with 0-hour expiry is already expired
    time.sleep(1)
    with pytest.raises(pyjwt.ExpiredSignatureError):
        validate_token(sc_root, token)


def test_invalid_token_rejected(sc_root):
    with pytest.raises(pyjwt.InvalidTokenError):
        validate_token(sc_root, "not.a.valid.token")


def test_get_username_from_token(sc_root):
    token = generate_token(sc_root, username="alice")
    assert get_username_from_token(sc_root, token) == "alice"


def test_get_username_invalid_returns_none(sc_root):
    assert get_username_from_token(sc_root, "bogus") is None


def test_extract_bearer_token():
    assert extract_bearer_token("Bearer abc123") == "abc123"
    assert extract_bearer_token("bearer ABC") == "ABC"
    assert extract_bearer_token("Basic abc123") is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token(None) is None


def test_token_contains_fingerprint(sc_root):
    # Create user with pubkey
    create_user(sc_root.parent, "bob", pubkey="ssh-ed25519 AAAA...")
    token = generate_token(sc_root, username="bob")
    payload = validate_token(sc_root, token)
    assert payload["fingerprint"] != ""
    assert len(payload["fingerprint"]) == 16


# ── Login code flow ─────────────────────────────────────────────

def test_login_code_roundtrip(sc_root):
    from scenecraft.vcs.auth import create_login_code, consume_login_code
    token = generate_token(sc_root, username="alice")
    code = create_login_code(sc_root, token)
    assert isinstance(code, str)
    assert len(code) > 20

    consumed = consume_login_code(sc_root, code)
    assert consumed == token


def test_login_code_is_single_use(sc_root):
    from scenecraft.vcs.auth import create_login_code, consume_login_code
    token = generate_token(sc_root, username="alice")
    code = create_login_code(sc_root, token)

    assert consume_login_code(sc_root, code) == token
    # Second consume returns None
    assert consume_login_code(sc_root, code) is None


def test_login_code_invalid(sc_root):
    from scenecraft.vcs.auth import consume_login_code
    assert consume_login_code(sc_root, "not-a-real-code") is None


def test_login_code_expired(sc_root):
    """Simulate an expired code by writing one directly with a past expiry."""
    from scenecraft.vcs.auth import _ensure_login_codes_table, consume_login_code
    conn = _ensure_login_codes_table(sc_root)
    conn.execute("INSERT INTO login_codes (code, token, expires_at) VALUES (?, ?, ?)", ("expired-code", "some-jwt", 0))
    conn.commit()
    conn.close()
    assert consume_login_code(sc_root, "expired-code") is None


# ── Cookie helpers ──────────────────────────────────────────────

def test_extract_cookie_token():
    from scenecraft.vcs.auth import extract_cookie_token
    assert extract_cookie_token("scenecraft_jwt=abc123") == "abc123"
    assert extract_cookie_token("foo=bar; scenecraft_jwt=xyz; other=thing") == "xyz"
    assert extract_cookie_token("unrelated=value") is None
    assert extract_cookie_token(None) is None
    assert extract_cookie_token("") is None


def test_build_cookie_header():
    from scenecraft.vcs.auth import build_cookie_header
    header = build_cookie_header("tok123", max_age_seconds=600)
    assert "scenecraft_jwt=tok123" in header
    assert "HttpOnly" in header
    assert "SameSite=Lax" in header
    assert "Max-Age=600" in header
    assert "Path=/" in header
    assert "Secure" not in header  # Default: not secure

    secure_header = build_cookie_header("tok", secure=True)
    assert "Secure" in secure_header


def test_build_clear_cookie_header():
    from scenecraft.vcs.auth import build_clear_cookie_header
    header = build_clear_cookie_header()
    assert "scenecraft_jwt=" in header
    assert "Max-Age=0" in header
