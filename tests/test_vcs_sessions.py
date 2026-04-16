"""Tests for scenecraft VCS session management."""

import sqlite3
from pathlib import Path

import pytest

from scenecraft.vcs.bootstrap import init_root, create_project, create_user, add_user_to_org
from scenecraft.vcs.objects import commit_working_copy, set_ref, get_ref
from scenecraft.vcs.sessions import (
    create_session,
    get_session,
    get_session_for_user,
    touch_session,
    list_sessions,
    prune_sessions,
    delete_session,
)


@pytest.fixture
def env(tmp_path):
    """Set up .scenecraft with org, user, project, and an initial commit."""
    init_root(tmp_path, org_name="testorg", admin_username="alice")
    create_user(tmp_path, "bob", role="editor")
    add_user_to_org(tmp_path, "testorg", "bob")
    project_dir = create_project(tmp_path, "testorg", "myproject")

    # Create a working DB and make an initial commit
    working_db = tmp_path / "seed.db"
    conn = sqlite3.connect(str(working_db))
    conn.execute("CREATE TABLE keyframes (id TEXT PRIMARY KEY, prompt TEXT)")
    conn.execute("INSERT INTO keyframes VALUES ('kf_abc', 'test prompt')")
    conn.commit()
    conn.close()

    commit = commit_working_copy(project_dir, working_db, "main", "alice", "Initial commit")

    sc_root = tmp_path / ".scenecraft"
    return {
        "tmp_path": tmp_path,
        "sc_root": sc_root,
        "project_dir": project_dir,
        "working_db": working_db,
        "initial_commit": commit,
    }


def test_create_session(env):
    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main"
    )
    assert session["username"] == "alice"
    assert session["branch"] == "main"
    assert session["commit_hash"] == env["initial_commit"]["hash"]
    assert session["reused"] is False

    wc = Path(session["working_copy"])
    assert wc.exists()

    # Verify the working copy has the committed data
    conn = sqlite3.connect(str(wc))
    row = conn.execute("SELECT prompt FROM keyframes WHERE id = 'kf_abc'").fetchone()
    conn.close()
    assert row[0] == "test prompt"


def test_session_reuse(env):
    s1 = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    s2 = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")

    assert s2["reused"] is True
    assert s2["id"] == s1["id"]
    assert s2["working_copy"] == s1["working_copy"]


def test_session_stale_when_branch_advances(env):
    s1 = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")

    # Make a new commit to advance main
    conn = sqlite3.connect(str(env["working_db"]))
    conn.execute("INSERT INTO keyframes VALUES ('kf_new', 'new prompt')")
    conn.commit()
    conn.close()
    commit_working_copy(env["project_dir"], env["working_db"], "main", "alice", "Second commit")

    # Creating session again should NOT reuse (branch advanced)
    s2 = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    assert s2["reused"] is False
    assert s2["id"] != s1["id"]

    # New working copy should have the new data
    conn = sqlite3.connect(s2["working_copy"])
    row = conn.execute("SELECT prompt FROM keyframes WHERE id = 'kf_new'").fetchone()
    conn.close()
    assert row[0] == "new prompt"


def test_multiple_users_isolated(env):
    s_alice = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    s_bob = create_session(env["sc_root"], "bob", "testorg", "myproject", env["project_dir"], "main")

    assert s_alice["working_copy"] != s_bob["working_copy"]
    assert Path(s_alice["working_copy"]).exists()
    assert Path(s_bob["working_copy"]).exists()

    # Modify alice's working copy — bob's should be unaffected
    conn = sqlite3.connect(s_alice["working_copy"])
    conn.execute("INSERT INTO keyframes VALUES ('kf_alice', 'alice only')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(s_bob["working_copy"])
    row = conn.execute("SELECT * FROM keyframes WHERE id = 'kf_alice'").fetchone()
    conn.close()
    assert row is None  # Bob doesn't have alice's change


def test_get_session(env):
    s = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    loaded = get_session(env["sc_root"], s["id"])
    assert loaded is not None
    assert loaded["id"] == s["id"]


def test_get_session_missing(env):
    assert get_session(env["sc_root"], "nonexistent") is None


def test_get_session_for_user(env):
    create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    loaded = get_session_for_user(env["sc_root"], "alice", "testorg", "myproject", "main")
    assert loaded is not None
    assert loaded["username"] == "alice"


def test_list_sessions(env):
    create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    create_session(env["sc_root"], "bob", "testorg", "myproject", env["project_dir"], "main")

    sessions = list_sessions(env["sc_root"])
    assert len(sessions) == 2


def test_delete_session(env):
    s = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")
    wc = Path(s["working_copy"])
    assert wc.exists()

    assert delete_session(env["sc_root"], s["id"]) is True
    assert not wc.exists()
    assert get_session(env["sc_root"], s["id"]) is None


def test_delete_session_missing(env):
    assert delete_session(env["sc_root"], "nonexistent") is False


def test_prune_sessions(env):
    s = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main")

    # Set last_active to 30 days ago
    from scenecraft.vcs.bootstrap import get_sessions_db
    from datetime import datetime, timezone, timedelta
    old_time = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    conn = get_sessions_db(env["sc_root"])
    conn.execute("UPDATE sessions SET last_active = ? WHERE id = ?", (old_time, s["id"]))
    conn.commit()
    conn.close()

    count = prune_sessions(env["sc_root"], max_age_days=7)
    assert count == 1
    assert not Path(s["working_copy"]).exists()
    assert get_session(env["sc_root"], s["id"]) is None


def test_get_db_with_session_path(env, tmp_path):
    """get_db(db_path=...) routes to the provided path, not project_dir/project.db."""
    from scenecraft.db import get_db, close_db

    # Create an isolated empty DB at a custom path
    custom = tmp_path / "custom-session.db"
    conn = get_db(env["project_dir"], db_path=custom)
    assert custom.exists()
    # The connection's filename should match the custom path
    result = conn.execute("PRAGMA database_list").fetchone()
    assert result["file"].endswith("custom-session.db")
    close_db(env["project_dir"], db_path=custom)


def test_branch_session(env):
    """User creates a session on a branch forked from main."""
    # Create branch ref pointing to main's tip
    main_tip = get_ref(env["project_dir"], "main")
    set_ref(env["project_dir"], "alice/feature", main_tip)

    s = create_session(env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "alice/feature")
    assert s["branch"] == "alice/feature"
    assert s["commit_hash"] == main_tip

    # Working copy should have the same data as main
    conn = sqlite3.connect(s["working_copy"])
    row = conn.execute("SELECT prompt FROM keyframes WHERE id = 'kf_abc'").fetchone()
    conn.close()
    assert row[0] == "test prompt"
