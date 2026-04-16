"""Session management for scenecraft VCS — per-user, per-branch working copies."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .bootstrap import get_sessions_db
from .objects import get_ref, copy_snapshot_to, load_snapshot


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def create_session(
    sc_root: Path,
    username: str,
    org: str,
    project: str,
    project_dir: Path,
    branch: str = "main",
) -> dict:
    """Create a new session with a working copy from the branch tip.

    If a valid session already exists for this user/project/branch and the
    branch hasn't advanced, reuses it.

    Returns the session dict.
    """
    conn = get_sessions_db(sc_root)

    # Check for existing session
    row = conn.execute(
        "SELECT id, commit_hash, working_copy FROM sessions WHERE username = ? AND org = ? AND project = ? AND branch = ?",
        (username, org, project, branch),
    ).fetchone()

    branch_tip = get_ref(project_dir, branch)

    if row:
        existing_wc = Path(row["working_copy"])
        if row["commit_hash"] == branch_tip and existing_wc.exists():
            # Reuse — branch hasn't advanced
            conn.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (_now(), row["id"]))
            conn.commit()
            conn.close()
            return {
                "id": row["id"],
                "username": username,
                "org": org,
                "project": project,
                "branch": branch,
                "commit_hash": row["commit_hash"],
                "working_copy": str(existing_wc),
                "reused": True,
            }
        else:
            # Stale — remove old session, create fresh
            if existing_wc.exists():
                existing_wc.unlink()
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            conn.commit()

    # Create working copy
    session_id = uuid.uuid4().hex[:12]
    wc_dir = sc_root / "users" / username / "sessions"
    wc_dir.mkdir(parents=True, exist_ok=True)
    wc_path = wc_dir / f"{project}--{branch.replace('/', '--')}.db"

    if branch_tip:
        # Copy from the branch tip commit's DB snapshot
        from .objects import get_commit
        commit = get_commit(project_dir, branch_tip)
        if commit:
            copy_snapshot_to(project_dir, commit["db_hash"], wc_path)
        else:
            # Commit referenced by ref doesn't exist — start empty
            _create_empty_db(wc_path)
    else:
        # No commits on this branch yet — check for legacy project.db
        legacy_db = project_dir.parent / project / "project.db"
        if not legacy_db.exists():
            # Try the project_dir parent patterns
            for candidate in [project_dir / "project.db", project_dir.parent / "project.db"]:
                if candidate.exists():
                    legacy_db = candidate
                    break
        if legacy_db.exists():
            shutil.copy2(str(legacy_db), str(wc_path))
        else:
            _create_empty_db(wc_path)

    # Record session
    conn.execute(
        "INSERT INTO sessions (id, username, org, project, branch, commit_hash, working_copy, created_at, last_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, username, org, project, branch, branch_tip, str(wc_path), _now(), _now()),
    )
    conn.commit()
    conn.close()

    return {
        "id": session_id,
        "username": username,
        "org": org,
        "project": project,
        "branch": branch,
        "commit_hash": branch_tip,
        "working_copy": str(wc_path),
        "reused": False,
    }


def get_session(sc_root: Path, session_id: str) -> dict | None:
    """Look up a session by ID."""
    conn = get_sessions_db(sc_root)
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def get_session_for_user(sc_root: Path, username: str, org: str, project: str, branch: str) -> dict | None:
    """Look up a session by user/project/branch."""
    conn = get_sessions_db(sc_root)
    row = conn.execute(
        "SELECT * FROM sessions WHERE username = ? AND org = ? AND project = ? AND branch = ?",
        (username, org, project, branch),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def touch_session(sc_root: Path, session_id: str) -> None:
    """Update last_active timestamp."""
    conn = get_sessions_db(sc_root)
    conn.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (_now(), session_id))
    conn.commit()
    conn.close()


def list_sessions(sc_root: Path) -> list[dict]:
    """List all active sessions."""
    conn = get_sessions_db(sc_root)
    rows = conn.execute("SELECT * FROM sessions ORDER BY last_active DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def prune_sessions(sc_root: Path, max_age_days: int = 7) -> int:
    """Delete sessions inactive for longer than max_age_days. Returns count deleted."""
    conn = get_sessions_db(sc_root)
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)).isoformat()

    rows = conn.execute("SELECT id, working_copy FROM sessions WHERE last_active < ?", (cutoff,)).fetchall()

    for row in rows:
        wc = Path(row["working_copy"])
        if wc.exists():
            wc.unlink()

    conn.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
    conn.commit()
    conn.close()

    return len(rows)


def delete_session(sc_root: Path, session_id: str) -> bool:
    """Delete a specific session and its working copy."""
    conn = get_sessions_db(sc_root)
    row = conn.execute("SELECT working_copy FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return False

    wc = Path(row["working_copy"])
    if wc.exists():
        wc.unlink()

    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return True


def _create_empty_db(path: Path) -> None:
    """Create an empty project DB with basic schema."""
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
