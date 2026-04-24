"""Bootstrap the .scenecraft directory structure and initialize databases."""

from __future__ import annotations

import getpass
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Schema definitions ───────────────────────────────────────────

SERVER_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    pubkey_fingerprint TEXT NOT NULL DEFAULT '',
    pubkey TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'editor',
    must_change_password INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orgs (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS org_members (
    org TEXT NOT NULL REFERENCES orgs(name),
    username TEXT NOT NULL REFERENCES users(username),
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (org, username)
);

-- M16: spend_ledger tracks every paid-service call across all plugins.
-- Unit-agnostic (credit / usd_micro / token / character / second) so future
-- paid plugins (Veo, Replicate, ElevenLabs, OpenAI) reuse it unchanged.
-- Aggregation always groups by `unit` — you can't SUM credits and dollars.
-- Source distinguishes BYO (key lives on this box) from broker (scenecraft.online
-- hosted the call). Per 2026-04-23 dev directive: auth FKs on username/org are
-- deferred; values default to '' until the auth milestone ships.
CREATE TABLE IF NOT EXISTS spend_ledger (
    id TEXT PRIMARY KEY,
    plugin_id TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    org TEXT NOT NULL DEFAULT '',
    api_key_id TEXT,
    amount INTEGER NOT NULL,
    unit TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    operation TEXT NOT NULL,
    job_ref TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user   ON spend_ledger(username, unit, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_org    ON spend_ledger(org, unit, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_plugin ON spend_ledger(plugin_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_unit   ON spend_ledger(unit, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_source ON spend_ledger(source, created_at);

-- M16: api_keys for paid-plugin double-gate auth.
-- Each key is scoped to a user. The raw key is shown once at issue time; only
-- the PBKDF2 hash is persisted. Revocation is soft (revoked_at timestamp).
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    key_hash     TEXT NOT NULL,
    salt         TEXT NOT NULL,
    issued_by    TEXT NOT NULL,
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    label        TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_username ON api_keys(username);
CREATE INDEX IF NOT EXISTS idx_api_keys_expires  ON api_keys(expires_at);
"""

ORG_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

USER_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_workspaces (
    name TEXT NOT NULL,
    project TEXT NOT NULL,
    layout TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name, project)
);
"""

SESSIONS_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    org TEXT NOT NULL,
    project TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    commit_hash TEXT NOT NULL DEFAULT '',
    working_copy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_active TEXT NOT NULL
);
"""


# ── Database helpers ─────────────────────────────────────────────

def _init_db(db_path: Path, schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.commit()
    return conn


def get_server_db(root: Path) -> sqlite3.Connection:
    conn = _init_db(root / "server.db", SERVER_DB_SCHEMA)
    # -- Migrations for existing databases --
    # Add must_change_password column to users if missing (added in M16 auth milestone).
    try:
        conn.execute("SELECT must_change_password FROM users LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn


def get_sessions_db(root: Path) -> sqlite3.Connection:
    return _init_db(root / "sessions.db", SESSIONS_DB_SCHEMA)


def get_org_db(org_dir: Path) -> sqlite3.Connection:
    return _init_db(org_dir / "org.db", ORG_DB_SCHEMA)


def get_user_db(user_dir: Path) -> sqlite3.Connection:
    return _init_db(user_dir / "user.db", USER_DB_SCHEMA)


def record_spend(
    root: Path,
    *,
    plugin_id: str,
    amount: int,
    unit: str,
    operation: str,
    username: str = "",
    org: str = "",
    api_key_id: str | None = None,
    job_ref: str | None = None,
    metadata: dict | None = None,
    source: str = "local",
) -> str:
    """Insert a spend_ledger row into server.db. Returns the ledger row id.

    This is the ONLY write path to spend_ledger. Per spec R9a, plugins must call
    this via plugin_api; they never INSERT directly.

    Per 2026-04-23 dev directive: username/org default to '' when auth is not
    yet wired. api_key_id stays None until the auth milestone ships.
    """
    import json
    import uuid
    from datetime import datetime, timezone

    # Trust boundary: plugin_id validation happens upstream in plugin_api —
    # this function trusts the caller. The narrow surface means only plugin_api
    # imports this, and plugin_api enforces the identity check before calling.
    ledger_id = f"spend_{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat()

    conn = get_server_db(root)
    conn.execute(
        """INSERT INTO spend_ledger (
            id, plugin_id, username, org, api_key_id, amount, unit,
            source, operation, job_ref, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ledger_id, plugin_id, username, org, api_key_id, amount, unit,
            source, operation, job_ref,
            json.dumps(metadata) if metadata is not None else None,
            created_at,
        ),
    )
    conn.commit()
    return ledger_id


def list_spend(
    root: Path,
    *,
    username: str | None = None,
    org: str | None = None,
    plugin_id: str | None = None,
    unit: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Query spend_ledger. Any argument None = unfiltered on that field.
    Returns list of dicts sorted by created_at DESC."""
    conn = get_server_db(root)
    clauses = []
    params: list = []
    if username is not None:
        clauses.append("username = ?")
        params.append(username)
    if org is not None:
        clauses.append("org = ?")
        params.append(org)
    if plugin_id is not None:
        clauses.append("plugin_id = ?")
        params.append(plugin_id)
    if unit is not None:
        clauses.append("unit = ?")
        params.append(unit)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM spend_ledger{where} ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Bootstrap ────────────────────────────────────────────────────

def find_root(start: Path | None = None) -> Path | None:
    """Locate the .scenecraft directory that owns the current working context.

    Resolution order:
    1. `SCENECRAFT_ROOT` env var — if set and points at a real `.scenecraft/` (or its
       parent), use it. Lets the user pin the server root without editing every call
       site; also how the test suite scopes each pytest tmp_path.
    2. Walk upward from `start` (or cwd) looking for a `.scenecraft/`.
    """
    import os as _os
    env_root = _os.environ.get("SCENECRAFT_ROOT", "").strip()
    if env_root:
        candidate = Path(env_root)
        # Accept either a path that IS the .scenecraft dir, or a parent containing one.
        if candidate.is_dir():
            if candidate.name == ".scenecraft":
                return candidate
            sub = candidate / ".scenecraft"
            if sub.is_dir():
                return sub
        # Env var set but invalid — fall through to cwd walk rather than error, so
        # deleting and re-init'ing still works.

    p = (start or Path.cwd()).resolve()
    while True:
        candidate = p / ".scenecraft"
        if candidate.is_dir():
            return candidate
        if p.parent == p:
            return None
        p = p.parent


def init_root(root: Path, org_name: str = "default", admin_username: str | None = None) -> Path:
    """Create the .scenecraft directory tree and initialize all databases.

    Returns the .scenecraft root path.
    """
    sc = root / ".scenecraft"
    if sc.exists():
        raise FileExistsError(f".scenecraft already exists at {sc}")

    now = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()
    username = admin_username or getpass.getuser()

    # Create directory tree
    _ensure_dir(sc)
    org_dir = _ensure_dir(sc / "orgs" / org_name)
    user_dir = _ensure_dir(sc / "users" / username)
    _ensure_dir(user_dir / "sessions")

    # Initialize server.db
    conn = get_server_db(sc)
    conn.execute(
        "INSERT INTO users (username, created_at, role) VALUES (?, ?, ?)",
        (username, now, "admin"),
    )
    conn.execute(
        "INSERT INTO orgs (name, created_at) VALUES (?, ?)",
        (org_name, now),
    )
    conn.execute(
        "INSERT INTO org_members (org, username, role, joined_at) VALUES (?, ?, ?, ?)",
        (org_name, username, "admin", now),
    )
    conn.commit()
    conn.close()

    # Initialize sessions.db
    get_sessions_db(sc).close()

    # Initialize org.db
    get_org_db(org_dir).close()

    # Initialize user.db
    get_user_db(user_dir).close()

    return sc


def create_org(root: Path, org_name: str) -> Path:
    """Create a new org directory and database."""
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    org_dir = _ensure_dir(sc / "orgs" / org_name)

    now = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()

    # Register in server.db
    conn = get_server_db(sc)
    conn.execute("INSERT INTO orgs (name, created_at) VALUES (?, ?)", (org_name, now))
    conn.commit()
    conn.close()

    # Initialize org.db
    get_org_db(org_dir).close()

    return org_dir


def create_user(root: Path, username: str, pubkey: str = "", role: str = "editor") -> Path:
    """Register a user and create their directory."""
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    user_dir = _ensure_dir(sc / "users" / username)
    _ensure_dir(user_dir / "sessions")

    now = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()

    fingerprint = ""
    if pubkey:
        fingerprint = hashlib.sha256(pubkey.encode()).hexdigest()[:16]

    conn = get_server_db(sc)
    conn.execute(
        "INSERT INTO users (username, pubkey_fingerprint, pubkey, created_at, role, must_change_password)"
        " VALUES (?, ?, ?, ?, ?, 1)",
        (username, fingerprint, pubkey, now, role),
    )
    conn.commit()
    conn.close()

    # Initialize user.db
    get_user_db(user_dir).close()

    return user_dir


def add_user_to_org(root: Path, org_name: str, username: str, role: str = "member") -> None:
    """Add a user to an org."""
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    now = datetime.now(tz=__import__('datetime').timezone.utc).isoformat()

    conn = get_server_db(sc)
    conn.execute(
        "INSERT INTO org_members (org, username, role, joined_at) VALUES (?, ?, ?, ?)",
        (org_name, username, role, now),
    )
    conn.commit()
    conn.close()


def create_project(root: Path, org_name: str, project_name: str) -> Path:
    """Create a project directory structure under an org."""
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    project_dir = sc / "orgs" / org_name / "projects" / project_name

    _ensure_dir(project_dir / "objects")
    _ensure_dir(project_dir / "refs")
    _ensure_dir(project_dir / "commits")
    _ensure_dir(project_dir / "assets" / "selected_keyframes")
    _ensure_dir(project_dir / "assets" / "keyframe_candidates")
    _ensure_dir(project_dir / "assets" / "transition_videos")

    # Create main ref (empty — no commits yet)
    (project_dir / "refs" / "main").write_text("")

    return project_dir


# ── Query helpers ────────────────────────────────────────────────

def list_orgs(root: Path) -> list[dict]:
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    conn = get_server_db(sc)
    rows = conn.execute("SELECT name, created_at FROM orgs ORDER BY name").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def list_users(root: Path) -> list[dict]:
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    conn = get_server_db(sc)
    rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def list_org_members(root: Path, org_name: str) -> list[dict]:
    sc = root / ".scenecraft" if not str(root).endswith(".scenecraft") else root
    conn = get_server_db(sc)
    rows = conn.execute(
        "SELECT username, role, joined_at FROM org_members WHERE org = ? ORDER BY username",
        (org_name,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]
