"""OAuth 2.0 client with PKCE for connecting to third-party services via agentbase.me.

Flow:
  1. Frontend calls /api/oauth/<service>/authorize → backend generates PKCE pair,
     stores pending state, returns authorization URL
  2. Browser redirects user to agentbase.me/oauth/authorize (consent page)
  3. User authorizes → agentbase.me redirects to /oauth/callback with code + state
  4. Backend verifies state, exchanges code + verifier for access/refresh tokens,
     persists them keyed by (user_id, service)
  5. Subsequent MCP connections use the stored access_token; refresh before expiry

Token storage: ~/.scenecraft/oauth-tokens.db (SQLite)
Pending-state storage: in-memory dict (short-lived, per-process)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [oauth] {msg}", file=sys.stderr, flush=True)


# ── Config ──────────────────────────────────────────────────────────


# agentbase.me is the authorization server for all registered OAuth clients.
# Override via env for dev/staging.
AGENTBASE_URL = os.environ.get("SCENECRAFT_AGENTBASE_URL", "https://agentbase.me")

# Scenecraft's registered client_id in agentbase.me's OAUTH_CLIENTS collection.
CLIENT_ID = os.environ.get("SCENECRAFT_OAUTH_CLIENT_ID", "scenecraft")

# Redirect URI — MUST match one of the URIs registered for CLIENT_ID.
# Prod: https://scenecraft.online/oauth/callback
# Dev: http://localhost:8890/oauth/callback
REDIRECT_URI = os.environ.get("SCENECRAFT_OAUTH_REDIRECT_URI", "http://localhost:8890/oauth/callback")


# Services we can connect to via OAuth. Each entry defines the resource server
# endpoint used once tokens are obtained. The OAuth flow itself is the same for
# every service — only the resource-server URL differs.
SERVICES: dict[str, dict[str, str]] = {
    "remember": {
        "label": "Remember",
        # SSE endpoint for the remember-mcp-server deployment.
        "mcp_url": os.environ.get(
            "SCENECRAFT_REMEMBER_MCP_URL",
            "https://remember-mcp-server-dit6gawkbq-uc.a.run.app/mcp",
        ),
        "scope": os.environ.get("SCENECRAFT_REMEMBER_SCOPE", ""),
    },
}


# ── Token storage ──────────────────────────────────────────────────


_DB_LOCK = threading.Lock()
_DB_PATH = Path.home() / ".scenecraft" / "oauth-tokens.db"


def _get_tokens_db() -> sqlite3.Connection:
    """Open (creating if needed) the OAuth tokens SQLite DB."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS oauth_tokens (
            user_id TEXT NOT NULL,
            service TEXT NOT NULL,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, service)
        )
    """)
    return conn


@dataclass
class StoredTokens:
    user_id: str
    service: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at.replace(tzinfo=self.expires_at.tzinfo or timezone.utc)

    def expires_soon(self, seconds: int = 300) -> bool:
        delta = (self.expires_at - datetime.now(timezone.utc)).total_seconds()
        return delta < seconds


def save_tokens(
    user_id: str,
    service: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
) -> StoredTokens:
    """Persist tokens for (user_id, service). Replaces any existing record."""
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + expires_in, tz=timezone.utc)
    with _DB_LOCK:
        conn = _get_tokens_db()
        existing = conn.execute(
            "SELECT created_at FROM oauth_tokens WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchone()
        created_at = existing["created_at"] if existing else now.isoformat()
        conn.execute(
            """
            INSERT INTO oauth_tokens (user_id, service, access_token, refresh_token, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, service) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (user_id, service, access_token, refresh_token, expires_at.isoformat(), created_at, now.isoformat()),
        )
        conn.commit()
        conn.close()
    return StoredTokens(
        user_id=user_id,
        service=service,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        created_at=datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at,
        updated_at=now,
    )


def load_tokens(user_id: str, service: str) -> StoredTokens | None:
    with _DB_LOCK:
        conn = _get_tokens_db()
        row = conn.execute(
            "SELECT * FROM oauth_tokens WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchone()
        conn.close()
    if not row:
        return None
    return StoredTokens(
        user_id=row["user_id"],
        service=row["service"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=datetime.fromisoformat(row["expires_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def delete_tokens(user_id: str, service: str) -> bool:
    with _DB_LOCK:
        conn = _get_tokens_db()
        cursor = conn.execute(
            "DELETE FROM oauth_tokens WHERE user_id = ? AND service = ?",
            (user_id, service),
        )
        conn.commit()
        deleted = cursor.rowcount > 0
        conn.close()
    return deleted


# ── PKCE helpers ────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ── Pending authorization state ─────────────────────────────────────


# Maps state-token → pending flow context. Expires after 10 minutes.
_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, dict] = {}
_STATE_TTL_SECONDS = 600


def create_pending_state(user_id: str, service: str, code_verifier: str, return_to: str | None = None) -> str:
    """Create a random state token and remember the context needed to complete the flow."""
    state = secrets.token_urlsafe(32)
    with _PENDING_LOCK:
        _PENDING[state] = {
            "user_id": user_id,
            "service": service,
            "code_verifier": code_verifier,
            "return_to": return_to,
            "created_at": time.time(),
        }
        # Opportunistic cleanup
        now = time.time()
        expired = [k for k, v in _PENDING.items() if now - v["created_at"] > _STATE_TTL_SECONDS]
        for k in expired:
            _PENDING.pop(k, None)
    return state


def consume_pending_state(state: str) -> dict | None:
    """Pop and return the pending context for a state token. Returns None if unknown/expired."""
    with _PENDING_LOCK:
        entry = _PENDING.pop(state, None)
    if entry is None:
        return None
    if time.time() - entry["created_at"] > _STATE_TTL_SECONDS:
        return None
    return entry


# ── Authorization URL + token exchange ──────────────────────────────


def build_authorize_url(service: str, state: str, code_challenge: str) -> str:
    """Build the consent URL the user should visit."""
    svc = SERVICES.get(service)
    if svc is None:
        raise ValueError(f"Unknown OAuth service: {service}")
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if svc.get("scope"):
        params["scope"] = svc["scope"]
    return f"{AGENTBASE_URL}/oauth/authorize?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }).encode("ascii")
    return _post_token_endpoint(body)


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access/refresh token pair."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode("ascii")
    return _post_token_endpoint(body)


def _post_token_endpoint(body: bytes) -> dict:
    req = urllib.request.Request(
        f"{AGENTBASE_URL}/api/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(err_body)
        except json.JSONDecodeError:
            err_data = {"error": "http_error", "error_description": err_body}
        raise TokenExchangeError(e.code, err_data) from e
    except urllib.error.URLError as e:
        raise TokenExchangeError(0, {"error": "network_error", "error_description": str(e)}) from e

    if "access_token" not in data:
        raise TokenExchangeError(500, data)
    return data


class TokenExchangeError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        super().__init__(f"Token exchange failed ({status}): {body}")


# ── Connection helper for MCP clients ───────────────────────────────


def get_valid_access_token(user_id: str, service: str) -> str | None:
    """Return a current access token for (user_id, service), refreshing if needed.

    Returns None if no tokens are stored or refresh fails and no refresh token is present.
    """
    tokens = load_tokens(user_id, service)
    if tokens is None:
        return None

    if not tokens.expires_soon():
        return tokens.access_token

    if not tokens.refresh_token:
        _log(f"Access token for {user_id}/{service} expiring soon but no refresh_token stored")
        return tokens.access_token if not tokens.is_expired() else None

    try:
        refreshed = refresh_access_token(tokens.refresh_token)
    except TokenExchangeError as e:
        _log(f"Refresh failed for {user_id}/{service}: {e.body}")
        return None

    new = save_tokens(
        user_id=user_id,
        service=service,
        access_token=refreshed["access_token"],
        refresh_token=refreshed.get("refresh_token") or tokens.refresh_token,
        expires_in=int(refreshed.get("expires_in", 3600)),
    )
    return new.access_token
