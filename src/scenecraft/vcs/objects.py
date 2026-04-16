"""Content-addressed object store and commit engine for scenecraft VCS."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ── Object Store ─────────────────────────────────────────────────

def store_snapshot(project_dir: Path, source_db: Path) -> str:
    """Snapshot a SQLite DB into the object store via sqlite3.backup().

    Returns the SHA-256 hash of the stored snapshot.
    If an object with the same hash already exists, deduplicates (skips write).
    """
    objects_dir = project_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)

    # Use sqlite3.backup() for a consistent snapshot (safe for WAL mode)
    with tempfile.NamedTemporaryFile(suffix=".db", dir=str(objects_dir), delete=False) as tmp:
        tmp_path = Path(tmp.name)

    src = sqlite3.connect(str(source_db))
    dst = sqlite3.connect(str(tmp_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    db_hash = _sha256_file(tmp_path)
    target = objects_dir / f"{db_hash}.db"

    if target.exists():
        # Dedup — identical snapshot already stored
        tmp_path.unlink()
    else:
        tmp_path.rename(target)

    return db_hash


def load_snapshot(project_dir: Path, db_hash: str) -> Path:
    """Return the path to a stored snapshot. Raises FileNotFoundError if missing."""
    path = project_dir / "objects" / f"{db_hash}.db"
    if not path.exists():
        raise FileNotFoundError(f"Object not found: {db_hash}")
    return path


def copy_snapshot_to(project_dir: Path, db_hash: str, dest: Path) -> None:
    """Copy a stored snapshot to a destination path (e.g., for creating a working copy)."""
    src = load_snapshot(project_dir, db_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))


# ── Commits ──────────────────────────────────────────────────────

def create_commit(
    project_dir: Path,
    db_hash: str,
    parents: list[str],
    author: str,
    message: str,
) -> dict:
    """Create a commit object and store it.

    Returns the full commit dict including the computed hash.
    """
    commits_dir = project_dir / "commits"
    commits_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=timezone.utc).isoformat()

    # Build metadata without hash (hash is derived from the rest)
    meta = {
        "db_hash": db_hash,
        "parents": parents,
        "author": author,
        "message": message,
        "timestamp": now,
    }

    # Canonical JSON for deterministic hashing
    canonical = json.dumps(meta, sort_keys=True, separators=(",", ":"))
    commit_hash = _sha256_str(canonical)

    commit = {"hash": commit_hash, **meta}

    commit_path = commits_dir / f"{commit_hash}.json"
    commit_path.write_text(json.dumps(commit, indent=2))

    return commit


def get_commit(project_dir: Path, commit_hash: str) -> dict | None:
    """Load a commit by hash. Returns None if not found."""
    path = project_dir / "commits" / f"{commit_hash}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def list_commits(project_dir: Path, head_hash: str, limit: int = 50) -> list[dict]:
    """Walk the parent chain from head_hash and return commits in reverse chronological order."""
    commits = []
    current = head_hash
    seen = set()

    while current and len(commits) < limit:
        if current in seen:
            break
        seen.add(current)

        commit = get_commit(project_dir, current)
        if commit is None:
            break
        commits.append(commit)

        # Follow first parent (linear history for now)
        parents = commit.get("parents", [])
        current = parents[0] if parents else None

    return commits


# ── Refs (Branch Pointers) ───────────────────────────────────────

def get_ref(project_dir: Path, branch: str) -> str:
    """Read a branch ref. Returns the commit hash, or empty string if branch has no commits."""
    # Support nested branch names like prmichaelsen/my-branch
    ref_path = project_dir / "refs" / branch
    if not ref_path.exists():
        return ""
    return ref_path.read_text().strip()


def set_ref(project_dir: Path, branch: str, commit_hash: str) -> None:
    """Update a branch ref to point to a commit."""
    ref_path = project_dir / "refs" / branch
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(commit_hash)


def list_refs(project_dir: Path) -> dict[str, str]:
    """List all branch refs. Returns {branch_name: commit_hash}."""
    refs_dir = project_dir / "refs"
    if not refs_dir.exists():
        return {}
    result = {}
    for path in refs_dir.rglob("*"):
        if path.is_file():
            branch = str(path.relative_to(refs_dir))
            result[branch] = path.read_text().strip()
    return result


# ── High-Level Operations ────────────────────────────────────────

def commit_working_copy(
    project_dir: Path,
    source_db: Path,
    branch: str,
    author: str,
    message: str,
) -> dict:
    """Snapshot the working copy, create a commit, and advance the branch ref.

    This is the main "commit" operation — equivalent to `git commit`.
    Returns the commit dict.
    """
    # 1. Snapshot the DB into objects/
    db_hash = store_snapshot(project_dir, source_db)

    # 2. Determine parent (current branch tip)
    parent = get_ref(project_dir, branch)
    parents = [parent] if parent else []

    # 3. Create commit
    commit = create_commit(project_dir, db_hash, parents, author, message)

    # 4. Advance branch ref
    set_ref(project_dir, branch, commit["hash"])

    return commit
