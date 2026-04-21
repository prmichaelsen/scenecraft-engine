"""Branch operations — create, list, checkout, delete."""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .bootstrap import get_sessions_db
from .objects import (
    get_commit,
    get_ref,
    list_refs,
    set_ref,
    store_snapshot,
)


# Branch names: segments of [A-Za-z0-9_-] joined by '/'. No empty segments,
# no leading/trailing '/', total length 1..100.
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")


class BranchError(Exception):
    """Raised for branch-operation validation or invariant failures."""


def validate_branch_name(name: str) -> None:
    """Raise BranchError if `name` is not a legal branch name."""
    if not isinstance(name, str) or not name:
        raise BranchError("Branch name is required")
    if len(name) > 100:
        raise BranchError("Branch name too long (max 100 chars)")
    if not _BRANCH_NAME_RE.match(name):
        raise BranchError(
            f"Invalid branch name: {name!r}. "
            "Use letters, digits, '_', '-', and '/' for namespacing."
        )


def _refs_dir(project_dir: Path) -> Path:
    return project_dir / "refs"


def _ref_path(project_dir: Path, branch: str) -> Path:
    return _refs_dir(project_dir) / branch


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def branch_exists(project_dir: Path, branch: str) -> bool:
    """Return True if a ref file exists for this branch."""
    return _ref_path(project_dir, branch).exists()


def create_branch(project_dir: Path, name: str, from_branch: str = "main") -> dict:
    """Create a new branch ref pointing to `from_branch`'s current commit.

    Does not touch session state or working copies — that's the caller's job
    (typically via checkout). Raises BranchError on validation failures or
    if the branch already exists.
    """
    validate_branch_name(name)

    if branch_exists(project_dir, name):
        raise BranchError(f"Branch already exists: {name}")

    if not branch_exists(project_dir, from_branch):
        raise BranchError(f"Source branch not found: {from_branch}")

    source_commit = get_ref(project_dir, from_branch)
    # Empty source is OK — the new branch just starts with no commits too.
    set_ref(project_dir, name, source_commit)

    return {"name": name, "commit_hash": source_commit, "from_branch": from_branch}


def list_branches(project_dir: Path, current_branch: str | None = None) -> list[dict]:
    """List all branches with their tip commit hashes.

    `current_branch` marks one branch as `isCurrent` in the output (optional).
    """
    refs = list_refs(project_dir)
    results = []
    for name in sorted(refs):
        results.append({
            "name": name,
            "commitHash": refs[name],
            "isCurrent": name == current_branch,
        })
    return results


def delete_branch(
    project_dir: Path,
    name: str,
    current_branch: str | None = None,
) -> None:
    """Delete a branch ref. Refuses to delete `main` or the currently checked-out branch."""
    validate_branch_name(name)

    if name == "main":
        raise BranchError("Cannot delete the 'main' branch")
    if current_branch is not None and name == current_branch:
        raise BranchError(f"Cannot delete the currently checked-out branch: {name}")

    ref_path = _ref_path(project_dir, name)
    if not ref_path.exists():
        raise BranchError(f"Branch not found: {name}")

    ref_path.unlink()

    # Best-effort: clean up empty parent directories (e.g. `refs/alice/` if empty
    # after deleting `alice/feature-branch`), but stop at refs/ itself.
    parent = ref_path.parent
    refs_root = _refs_dir(project_dir)
    while parent != refs_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def has_uncommitted_changes(project_dir: Path, session: dict) -> bool:
    """Return True if the session's working copy differs from its recorded commit.

    Compares the working DB hash (via sqlite3.backup) against the commit's
    stored `db_hash`. No commit on the branch yet → any working copy state is
    "uncommitted".
    """
    session_commit = session.get("commit_hash") or ""
    if not session_commit:
        # No baseline to compare against — caller gets to decide (treat as uncommitted
        # when deciding whether to warn on checkout).
        return True

    commit = get_commit(project_dir, session_commit)
    if commit is None:
        # Dangling session commit — safest to treat as uncommitted.
        return True

    working_copy = Path(session["working_copy"])
    if not working_copy.exists():
        return False

    # store_snapshot computes a hash but also writes to objects/ (deduped if
    # identical). For a pure comparison we want to avoid the side-effect write,
    # but the dedup makes it harmless — identical bytes → no new file. Use it
    # to get the consistent-snapshot hash.
    current_hash = store_snapshot(project_dir, working_copy)
    return current_hash != commit["db_hash"]


def checkout_branch(
    sc_root: Path,
    session_id: str,
    target_branch: str,
    project_dir: Path,
    force: bool = False,
) -> dict:
    """Switch the session's active branch.

    Updates the session's `branch`, `commit_hash`, and creates/reuses a working
    copy matching the target branch's tip commit.

    If the current working copy has uncommitted changes and `force=False`,
    raises BranchError without touching anything.

    Returns the updated session dict.
    """
    validate_branch_name(target_branch)

    if not branch_exists(project_dir, target_branch):
        raise BranchError(f"Branch not found: {target_branch}")

    conn = get_sessions_db(sc_root)
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        raise BranchError(f"Session not found: {session_id}")

    session = dict(row)

    # Uncommitted change guard
    if not force and has_uncommitted_changes(project_dir, session):
        conn.close()
        raise BranchError(
            "Working copy has uncommitted changes. Commit or pass force=True to discard."
        )

    # Compute target working copy path
    username = session["username"]
    project = session["project"]
    wc_dir = sc_root / "users" / username / "sessions"
    wc_dir.mkdir(parents=True, exist_ok=True)
    new_wc_path = wc_dir / f"{project}--{target_branch.replace('/', '--')}.db"

    target_tip = get_ref(project_dir, target_branch)

    # Populate the working copy from the target branch's tip commit
    if target_tip:
        commit = get_commit(project_dir, target_tip)
        if commit is None:
            conn.close()
            raise BranchError(
                f"Branch {target_branch} points to missing commit {target_tip}"
            )
        from .objects import copy_snapshot_to
        copy_snapshot_to(project_dir, commit["db_hash"], new_wc_path)
    else:
        # No commits on target branch yet — start with an empty DB so the working
        # copy file exists (avoids downstream "file missing" errors).
        _create_empty_db(new_wc_path)

    # If the old working copy is different from the new one, remove it. (Same path
    # means we just overwrote it — leave alone.)
    old_wc = Path(session["working_copy"])
    if old_wc != new_wc_path and old_wc.exists():
        try:
            old_wc.unlink()
        except OSError:
            # Non-fatal — we succeeded in creating the new copy; old one just lingers.
            pass

    now = _now()
    conn.execute(
        "UPDATE sessions "
        "SET branch = ?, commit_hash = ?, working_copy = ?, last_active = ? "
        "WHERE id = ?",
        (target_branch, target_tip, str(new_wc_path), now, session_id),
    )
    conn.commit()

    # Re-read to return the canonical row
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row)


def _create_empty_db(path: Path) -> None:
    """Create an empty project DB. Mirrors sessions._create_empty_db."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    conn.close()
