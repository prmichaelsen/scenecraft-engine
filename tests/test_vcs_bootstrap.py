"""Tests for scenecraft VCS bootstrap — directory structure and DB initialization."""

import sqlite3
from pathlib import Path

import pytest

from scenecraft.vcs.bootstrap import (
    init_root,
    find_root,
    create_org,
    create_user,
    add_user_to_org,
    create_project,
    list_orgs,
    list_users,
    list_org_members,
    get_server_db,
)


@pytest.fixture
def tmp_root(tmp_path):
    """Create a temp directory and init .scenecraft in it."""
    init_root(tmp_path, org_name="test-org", admin_username="testadmin")
    return tmp_path


def test_init_creates_directory_tree(tmp_root):
    sc = tmp_root / ".scenecraft"
    assert sc.is_dir()
    assert (sc / "server.db").is_file()
    assert (sc / "sessions.db").is_file()
    assert (sc / "orgs" / "test-org" / "org.db").is_file()
    assert (sc / "users" / "testadmin" / "user.db").is_file()
    assert (sc / "users" / "testadmin" / "sessions").is_dir()


def test_init_server_db_schema(tmp_root):
    conn = sqlite3.connect(str(tmp_root / ".scenecraft" / "server.db"))
    conn.row_factory = sqlite3.Row

    # Check tables exist
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "users" in tables
    assert "orgs" in tables
    assert "org_members" in tables

    # Check admin user
    user = conn.execute("SELECT * FROM users WHERE username = 'testadmin'").fetchone()
    assert user is not None
    assert user["role"] == "admin"

    # Check org
    org = conn.execute("SELECT * FROM orgs WHERE name = 'test-org'").fetchone()
    assert org is not None

    # Check membership
    member = conn.execute("SELECT * FROM org_members WHERE org = 'test-org' AND username = 'testadmin'").fetchone()
    assert member is not None
    assert member["role"] == "admin"

    conn.close()


def test_init_double_init_raises(tmp_root):
    with pytest.raises(FileExistsError):
        init_root(tmp_root, org_name="another")


def test_find_root(tmp_root):
    # From the root itself
    found = find_root(tmp_root)
    assert found == tmp_root / ".scenecraft"

    # From a subdirectory
    sub = tmp_root / "some" / "nested" / "dir"
    sub.mkdir(parents=True)
    found = find_root(sub)
    assert found == tmp_root / ".scenecraft"


def test_find_root_returns_none(tmp_path):
    # No .scenecraft anywhere
    assert find_root(tmp_path) is None


def test_create_org(tmp_root):
    create_org(tmp_root, "new-org")
    sc = tmp_root / ".scenecraft"
    assert (sc / "orgs" / "new-org" / "org.db").is_file()

    orgs = list_orgs(tmp_root)
    names = [o["name"] for o in orgs]
    assert "new-org" in names


def test_create_user(tmp_root):
    create_user(tmp_root, "jane", pubkey="ssh-ed25519 AAAA...", role="editor")
    sc = tmp_root / ".scenecraft"
    assert (sc / "users" / "jane" / "user.db").is_file()
    assert (sc / "users" / "jane" / "sessions").is_dir()

    users = list_users(tmp_root)
    names = [u["username"] for u in users]
    assert "jane" in names


def test_add_user_to_org(tmp_root):
    create_user(tmp_root, "bob")
    add_user_to_org(tmp_root, "test-org", "bob", role="editor")

    members = list_org_members(tmp_root, "test-org")
    names = [m["username"] for m in members]
    assert "bob" in names


def test_create_project(tmp_root):
    project_dir = create_project(tmp_root, "test-org", "my-video")
    assert project_dir.is_dir()
    assert (project_dir / "objects").is_dir()
    assert (project_dir / "refs").is_dir()
    assert (project_dir / "refs" / "main").is_file()
    assert (project_dir / "commits").is_dir()
    assert (project_dir / "assets" / "selected_keyframes").is_dir()
    assert (project_dir / "assets" / "keyframe_candidates").is_dir()
    assert (project_dir / "assets" / "transition_videos").is_dir()


def test_sessions_db_schema(tmp_root):
    conn = sqlite3.connect(str(tmp_root / ".scenecraft" / "sessions.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "sessions" in tables
    conn.close()


def test_org_db_schema(tmp_root):
    conn = sqlite3.connect(str(tmp_root / ".scenecraft" / "orgs" / "test-org" / "org.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "settings" in tables
    conn.close()


def test_user_db_schema(tmp_root):
    conn = sqlite3.connect(str(tmp_root / ".scenecraft" / "users" / "testadmin" / "user.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "preferences" in tables
    assert "saved_workspaces" in tables
    conn.close()
