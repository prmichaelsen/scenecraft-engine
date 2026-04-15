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
    role TEXT NOT NULL DEFAULT 'editor'
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
    return _init_db(root / "server.db", SERVER_DB_SCHEMA)


def get_sessions_db(root: Path) -> sqlite3.Connection:
    return _init_db(root / "sessions.db", SESSIONS_DB_SCHEMA)


def get_org_db(org_dir: Path) -> sqlite3.Connection:
    return _init_db(org_dir / "org.db", ORG_DB_SCHEMA)


def get_user_db(user_dir: Path) -> sqlite3.Connection:
    return _init_db(user_dir / "user.db", USER_DB_SCHEMA)


# ── Bootstrap ────────────────────────────────────────────────────

def find_root(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for a .scenecraft directory."""
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
        "INSERT INTO users (username, pubkey_fingerprint, pubkey, created_at, role) VALUES (?, ?, ?, ?, ?)",
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
