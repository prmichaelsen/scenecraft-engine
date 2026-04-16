"""Tests for scenecraft VCS object store and commit engine."""

import json
import sqlite3
from pathlib import Path

import pytest

from scenecraft.vcs.bootstrap import init_root, create_project
from scenecraft.vcs.objects import (
    store_snapshot,
    load_snapshot,
    copy_snapshot_to,
    create_commit,
    get_commit,
    list_commits,
    get_ref,
    set_ref,
    list_refs,
    commit_working_copy,
    _sha256_file,
    _sha256_str,
)


@pytest.fixture
def project_env(tmp_path):
    """Set up a .scenecraft root with a project and a working copy DB."""
    init_root(tmp_path, org_name="testorg", admin_username="alice")
    project_dir = create_project(tmp_path, "testorg", "myproject")

    # Create a working copy DB with some data
    working_db = tmp_path / "working.db"
    conn = sqlite3.connect(str(working_db))
    conn.execute("CREATE TABLE keyframes (id TEXT PRIMARY KEY, prompt TEXT)")
    conn.execute("INSERT INTO keyframes VALUES ('kf_abc', 'sunset over mountains')")
    conn.commit()
    conn.close()

    return project_dir, working_db


# ── Object Store ─────────────────────────────────────────────────

def test_store_snapshot_creates_object(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)

    assert len(db_hash) == 64  # SHA-256 hex
    assert (project_dir / "objects" / f"{db_hash}.db").exists()


def test_store_snapshot_dedup(project_env):
    project_dir, working_db = project_env
    hash1 = store_snapshot(project_dir, working_db)
    hash2 = store_snapshot(project_dir, working_db)

    assert hash1 == hash2
    # Only one file in objects/
    objects = list((project_dir / "objects").glob("*.db"))
    assert len(objects) == 1


def test_store_snapshot_different_data(project_env):
    project_dir, working_db = project_env
    hash1 = store_snapshot(project_dir, working_db)

    # Modify the working DB
    conn = sqlite3.connect(str(working_db))
    conn.execute("INSERT INTO keyframes VALUES ('kf_def', 'sunrise over ocean')")
    conn.commit()
    conn.close()

    hash2 = store_snapshot(project_dir, working_db)
    assert hash1 != hash2
    assert len(list((project_dir / "objects").glob("*.db"))) == 2


def test_load_snapshot(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)

    path = load_snapshot(project_dir, db_hash)
    assert path.exists()
    assert path.name == f"{db_hash}.db"


def test_load_snapshot_missing(project_env):
    project_dir, _ = project_env
    with pytest.raises(FileNotFoundError):
        load_snapshot(project_dir, "nonexistent")


def test_copy_snapshot_to(project_env, tmp_path):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)

    dest = tmp_path / "sessions" / "copy.db"
    copy_snapshot_to(project_dir, db_hash, dest)

    assert dest.exists()
    # Verify the copy has the same data
    conn = sqlite3.connect(str(dest))
    row = conn.execute("SELECT prompt FROM keyframes WHERE id = 'kf_abc'").fetchone()
    conn.close()
    assert row[0] == "sunset over mountains"


# ── Commits ──────────────────────────────────────────────────────

def test_create_commit(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)

    commit = create_commit(project_dir, db_hash, [], "alice", "Initial commit")

    assert "hash" in commit
    assert len(commit["hash"]) == 64
    assert commit["db_hash"] == db_hash
    assert commit["parents"] == []
    assert commit["author"] == "alice"
    assert commit["message"] == "Initial commit"
    assert "timestamp" in commit

    # Verify file exists
    assert (project_dir / "commits" / f"{commit['hash']}.json").exists()


def test_commit_hash_is_deterministic(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)

    # The hash should be deterministic from the metadata
    commit = get_commit(project_dir, create_commit(project_dir, db_hash, [], "alice", "test")["hash"])
    meta = {
        "db_hash": commit["db_hash"],
        "parents": commit["parents"],
        "author": commit["author"],
        "message": commit["message"],
        "timestamp": commit["timestamp"],
    }
    expected_hash = _sha256_str(json.dumps(meta, sort_keys=True, separators=(",", ":")))
    assert commit["hash"] == expected_hash


def test_get_commit(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)
    commit = create_commit(project_dir, db_hash, [], "alice", "test")

    loaded = get_commit(project_dir, commit["hash"])
    assert loaded == commit


def test_get_commit_missing(project_env):
    project_dir, _ = project_env
    assert get_commit(project_dir, "nonexistent") is None


def test_list_commits_chain(project_env):
    project_dir, working_db = project_env

    # Create a chain of 3 commits
    db_hash = store_snapshot(project_dir, working_db)
    c1 = create_commit(project_dir, db_hash, [], "alice", "First")

    conn = sqlite3.connect(str(working_db))
    conn.execute("INSERT INTO keyframes VALUES ('kf_2', 'second')")
    conn.commit()
    conn.close()
    db_hash2 = store_snapshot(project_dir, working_db)
    c2 = create_commit(project_dir, db_hash2, [c1["hash"]], "alice", "Second")

    conn = sqlite3.connect(str(working_db))
    conn.execute("INSERT INTO keyframes VALUES ('kf_3', 'third')")
    conn.commit()
    conn.close()
    db_hash3 = store_snapshot(project_dir, working_db)
    c3 = create_commit(project_dir, db_hash3, [c2["hash"]], "alice", "Third")

    # Walk from c3 back
    history = list_commits(project_dir, c3["hash"])
    assert len(history) == 3
    assert history[0]["hash"] == c3["hash"]
    assert history[1]["hash"] == c2["hash"]
    assert history[2]["hash"] == c1["hash"]


def test_list_commits_with_limit(project_env):
    project_dir, working_db = project_env
    db_hash = store_snapshot(project_dir, working_db)
    c1 = create_commit(project_dir, db_hash, [], "alice", "First")
    c2 = create_commit(project_dir, db_hash, [c1["hash"]], "alice", "Second")
    c3 = create_commit(project_dir, db_hash, [c2["hash"]], "alice", "Third")

    history = list_commits(project_dir, c3["hash"], limit=2)
    assert len(history) == 2


# ── Refs ─────────────────────────────────────────────────────────

def test_set_and_get_ref(project_env):
    project_dir, _ = project_env
    set_ref(project_dir, "main", "abc123")
    assert get_ref(project_dir, "main") == "abc123"


def test_get_ref_empty(project_env):
    project_dir, _ = project_env
    # main ref was created empty by create_project
    assert get_ref(project_dir, "main") == ""


def test_get_ref_missing(project_env):
    project_dir, _ = project_env
    assert get_ref(project_dir, "nonexistent") == ""


def test_nested_ref(project_env):
    project_dir, _ = project_env
    set_ref(project_dir, "alice/feature-branch", "def456")
    assert get_ref(project_dir, "alice/feature-branch") == "def456"


def test_list_refs(project_env):
    project_dir, _ = project_env
    set_ref(project_dir, "main", "aaa")
    set_ref(project_dir, "alice/branch1", "bbb")
    set_ref(project_dir, "bob/branch2", "ccc")

    refs = list_refs(project_dir)
    assert refs["main"] == "aaa"
    assert refs["alice/branch1"] == "bbb"
    assert refs["bob/branch2"] == "ccc"


# ── High-Level: commit_working_copy ──────────────────────────────

def test_commit_working_copy(project_env):
    project_dir, working_db = project_env

    commit = commit_working_copy(project_dir, working_db, "main", "alice", "Initial commit")

    assert commit["author"] == "alice"
    assert commit["message"] == "Initial commit"
    assert commit["parents"] == []
    assert get_ref(project_dir, "main") == commit["hash"]

    # Verify the snapshot exists
    assert (project_dir / "objects" / f"{commit['db_hash']}.db").exists()


def test_commit_chain_advances_ref(project_env):
    project_dir, working_db = project_env

    c1 = commit_working_copy(project_dir, working_db, "main", "alice", "First")
    assert get_ref(project_dir, "main") == c1["hash"]

    # Modify and commit again
    conn = sqlite3.connect(str(working_db))
    conn.execute("INSERT INTO keyframes VALUES ('kf_new', 'new prompt')")
    conn.commit()
    conn.close()

    c2 = commit_working_copy(project_dir, working_db, "main", "alice", "Second")
    assert get_ref(project_dir, "main") == c2["hash"]
    assert c2["parents"] == [c1["hash"]]


def test_commit_on_branch(project_env):
    project_dir, working_db = project_env

    # Commit to main first
    c1 = commit_working_copy(project_dir, working_db, "main", "alice", "Main commit")

    # Create a branch ref from main
    set_ref(project_dir, "alice/feature", c1["hash"])

    # Commit to branch
    conn = sqlite3.connect(str(working_db))
    conn.execute("INSERT INTO keyframes VALUES ('kf_branch', 'branch work')")
    conn.commit()
    conn.close()

    c2 = commit_working_copy(project_dir, working_db, "alice/feature", "alice", "Branch commit")

    # Branch advanced, main didn't
    assert get_ref(project_dir, "alice/feature") == c2["hash"]
    assert get_ref(project_dir, "main") == c1["hash"]
    assert c2["parents"] == [c1["hash"]]
