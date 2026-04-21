"""Tests for scenecraft VCS branch operations."""

import sqlite3

import pytest

from scenecraft.vcs.bootstrap import (
    init_root,
    create_project,
)
from scenecraft.vcs.objects import commit_working_copy, get_ref
from scenecraft.vcs.sessions import create_session
from scenecraft.vcs.branches import (
    BranchError,
    branch_exists,
    checkout_branch,
    create_branch,
    delete_branch,
    has_uncommitted_changes,
    list_branches,
    validate_branch_name,
)


@pytest.fixture
def env(tmp_path):
    """Set up .scenecraft with org, user, project, and an initial commit on main."""
    init_root(tmp_path, org_name="testorg", admin_username="alice")
    project_dir = create_project(tmp_path, "testorg", "myproject")

    working_db = tmp_path / "seed.db"
    conn = sqlite3.connect(str(working_db))
    conn.execute("CREATE TABLE keyframes (id TEXT PRIMARY KEY, prompt TEXT)")
    conn.execute("INSERT INTO keyframes VALUES ('kf_abc', 'initial')")
    conn.commit()
    conn.close()

    commit = commit_working_copy(project_dir, working_db, "main", "alice", "Initial commit")

    return {
        "tmp_path": tmp_path,
        "sc_root": tmp_path / ".scenecraft",
        "project_dir": project_dir,
        "working_db": working_db,
        "initial_commit": commit,
    }


# ── validate_branch_name ─────────────────────────────────────────

def test_validate_branch_name_accepts_simple():
    validate_branch_name("main")
    validate_branch_name("feature-1")
    validate_branch_name("alice/color-pass")
    validate_branch_name("alice/feat/deep-nesting")


def test_validate_branch_name_rejects_empty():
    with pytest.raises(BranchError):
        validate_branch_name("")


def test_validate_branch_name_rejects_spaces():
    with pytest.raises(BranchError):
        validate_branch_name("my branch")


def test_validate_branch_name_rejects_special_chars():
    for bad in ["feat..1", "feat@1", "feat 1", "feat#1", "feat:1"]:
        with pytest.raises(BranchError):
            validate_branch_name(bad)


def test_validate_branch_name_rejects_leading_or_trailing_slash():
    with pytest.raises(BranchError):
        validate_branch_name("/feature")
    with pytest.raises(BranchError):
        validate_branch_name("feature/")


def test_validate_branch_name_rejects_double_slash():
    with pytest.raises(BranchError):
        validate_branch_name("alice//feat")


def test_validate_branch_name_rejects_too_long():
    with pytest.raises(BranchError):
        validate_branch_name("a" * 101)


# ── create_branch ────────────────────────────────────────────────

def test_create_branch_from_main(env):
    main_hash = get_ref(env["project_dir"], "main")
    result = create_branch(env["project_dir"], "feature-1")

    assert result["name"] == "feature-1"
    assert result["commit_hash"] == main_hash
    assert result["from_branch"] == "main"
    assert branch_exists(env["project_dir"], "feature-1")
    assert get_ref(env["project_dir"], "feature-1") == main_hash


def test_create_nested_branch(env):
    result = create_branch(env["project_dir"], "alice/color-pass")
    assert branch_exists(env["project_dir"], "alice/color-pass")
    assert result["name"] == "alice/color-pass"


def test_create_branch_from_custom_source(env):
    # First create and advance another branch
    create_branch(env["project_dir"], "source-branch")
    conn = sqlite3.connect(str(env["working_db"]))
    conn.execute("INSERT INTO keyframes VALUES ('kf_new', 'new')")
    conn.commit()
    conn.close()
    new_commit = commit_working_copy(
        env["project_dir"], env["working_db"], "source-branch", "alice", "Advance source"
    )

    # Branch from source-branch — should inherit its tip, not main's
    result = create_branch(env["project_dir"], "child", from_branch="source-branch")
    assert result["commit_hash"] == new_commit["hash"]
    assert result["commit_hash"] != get_ref(env["project_dir"], "main")


def test_create_branch_duplicate_fails(env):
    create_branch(env["project_dir"], "feature-1")
    with pytest.raises(BranchError, match="already exists"):
        create_branch(env["project_dir"], "feature-1")


def test_create_branch_from_missing_source_fails(env):
    with pytest.raises(BranchError, match="Source branch not found"):
        create_branch(env["project_dir"], "child", from_branch="no-such-branch")


def test_create_branch_validates_name(env):
    with pytest.raises(BranchError):
        create_branch(env["project_dir"], "bad name")


# ── list_branches ────────────────────────────────────────────────

def test_list_branches_includes_main(env):
    branches = list_branches(env["project_dir"])
    names = [b["name"] for b in branches]
    assert "main" in names


def test_list_branches_returns_commit_hashes(env):
    main_hash = get_ref(env["project_dir"], "main")
    create_branch(env["project_dir"], "alice/feat")
    branches = list_branches(env["project_dir"])
    by_name = {b["name"]: b for b in branches}
    assert by_name["main"]["commitHash"] == main_hash
    assert by_name["alice/feat"]["commitHash"] == main_hash


def test_list_branches_marks_current(env):
    create_branch(env["project_dir"], "feat-x")
    branches = list_branches(env["project_dir"], current_branch="feat-x")
    by_name = {b["name"]: b for b in branches}
    assert by_name["feat-x"]["isCurrent"] is True
    assert by_name["main"]["isCurrent"] is False


def test_list_branches_lists_nested(env):
    create_branch(env["project_dir"], "alice/one")
    create_branch(env["project_dir"], "alice/two")
    create_branch(env["project_dir"], "bob/feat")
    names = {b["name"] for b in list_branches(env["project_dir"])}
    assert {"main", "alice/one", "alice/two", "bob/feat"} <= names


# ── delete_branch ────────────────────────────────────────────────

def test_delete_branch_removes_ref(env):
    create_branch(env["project_dir"], "throwaway")
    assert branch_exists(env["project_dir"], "throwaway")
    delete_branch(env["project_dir"], "throwaway")
    assert not branch_exists(env["project_dir"], "throwaway")


def test_delete_branch_cleans_empty_parent_dirs(env):
    create_branch(env["project_dir"], "alice/feature")
    delete_branch(env["project_dir"], "alice/feature")
    # alice/ dir should be cleaned up since it's empty
    assert not (env["project_dir"] / "refs" / "alice").exists()
    # refs/ root itself must still exist
    assert (env["project_dir"] / "refs").exists()


def test_delete_branch_preserves_parent_with_siblings(env):
    create_branch(env["project_dir"], "alice/one")
    create_branch(env["project_dir"], "alice/two")
    delete_branch(env["project_dir"], "alice/one")
    # alice/ still holds `two`
    assert (env["project_dir"] / "refs" / "alice" / "two").exists()


def test_delete_main_forbidden(env):
    with pytest.raises(BranchError, match="main"):
        delete_branch(env["project_dir"], "main")


def test_delete_current_branch_forbidden(env):
    create_branch(env["project_dir"], "active")
    with pytest.raises(BranchError, match="currently checked-out"):
        delete_branch(env["project_dir"], "active", current_branch="active")


def test_delete_nonexistent_branch_fails(env):
    with pytest.raises(BranchError, match="not found"):
        delete_branch(env["project_dir"], "no-such-branch")


# ── has_uncommitted_changes ──────────────────────────────────────

def test_no_uncommitted_changes_after_commit(env):
    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )
    # Session was created from the main tip → no divergence
    assert has_uncommitted_changes(env["project_dir"], session) is False


def test_uncommitted_changes_detected_after_modification(env):
    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )

    # Modify the working copy
    conn = sqlite3.connect(session["working_copy"])
    conn.execute("INSERT INTO keyframes VALUES ('kf_new', 'after commit')")
    conn.commit()
    conn.close()

    assert has_uncommitted_changes(env["project_dir"], session) is True


def test_uncommitted_changes_when_no_baseline(env):
    # Create a branch with no commits by setting a fake ref from main, then
    # forcing the session to sit on an empty baseline.
    session = {
        "commit_hash": "",
        "working_copy": str(env["working_db"]),
    }
    # No baseline to compare → treat as uncommitted
    assert has_uncommitted_changes(env["project_dir"], session) is True


# ── checkout_branch ──────────────────────────────────────────────

def test_checkout_switches_branch(env):
    create_branch(env["project_dir"], "alice/feat")

    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )

    updated = checkout_branch(
        env["sc_root"], session["id"], "alice/feat", env["project_dir"],
    )
    assert updated["branch"] == "alice/feat"
    assert updated["commit_hash"] == get_ref(env["project_dir"], "alice/feat")


def test_checkout_creates_working_copy_for_target(env):
    create_branch(env["project_dir"], "alice/feat")

    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )

    updated = checkout_branch(
        env["sc_root"], session["id"], "alice/feat", env["project_dir"],
    )
    # New working copy exists
    from pathlib import Path
    assert Path(updated["working_copy"]).exists()
    # Path encodes the nested branch safely
    assert "alice--feat" in updated["working_copy"]


def test_checkout_blocks_on_uncommitted_without_force(env):
    create_branch(env["project_dir"], "alice/feat")

    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )

    # Dirty the working copy
    conn = sqlite3.connect(session["working_copy"])
    conn.execute("INSERT INTO keyframes VALUES ('kf_dirty', 'unsaved')")
    conn.commit()
    conn.close()

    with pytest.raises(BranchError, match="uncommitted"):
        checkout_branch(
            env["sc_root"], session["id"], "alice/feat", env["project_dir"], force=False,
        )


def test_checkout_force_discards_uncommitted(env):
    create_branch(env["project_dir"], "alice/feat")

    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )

    # Dirty the working copy
    conn = sqlite3.connect(session["working_copy"])
    conn.execute("INSERT INTO keyframes VALUES ('kf_dirty', 'unsaved')")
    conn.commit()
    conn.close()

    # Should succeed with force=True
    updated = checkout_branch(
        env["sc_root"], session["id"], "alice/feat", env["project_dir"], force=True,
    )
    assert updated["branch"] == "alice/feat"


def test_checkout_nonexistent_branch_fails(env):
    session = create_session(
        env["sc_root"], "alice", "testorg", "myproject", env["project_dir"], "main",
    )
    with pytest.raises(BranchError, match="not found"):
        checkout_branch(
            env["sc_root"], session["id"], "no-such-branch", env["project_dir"],
        )


def test_checkout_unknown_session_fails(env):
    create_branch(env["project_dir"], "alice/feat")
    with pytest.raises(BranchError, match="Session not found"):
        checkout_branch(
            env["sc_root"], "bogus-session-id", "alice/feat", env["project_dir"],
        )
