"""Tests for scenecraft top-level CLI commands (init, token, org, user, session).

Tests are isolated via the `SCENECRAFT_ROOT` env var so they never escape their
pytest tmp_path and never touch any real `.scenecraft` the developer may have on
their filesystem. A couple of tests also exercise the explicit `--root` flag as
an additional isolation mechanism.
"""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from scenecraft.vcs.cli import register_commands


@pytest.fixture
def cli():
    """Build a root group with all top-level commands registered (mirrors scenecraft.cli:main)."""
    @click.group()
    def root():
        pass
    register_commands(root)
    return root


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized(tmp_path, runner, cli, monkeypatch):
    """Run init and pin SCENECRAFT_ROOT to the fresh tmp_path.

    Setting the env var here makes every subsequent CLI invocation in the test
    resolve to this specific .scenecraft, regardless of cwd or any real
    .scenecraft on disk.
    """
    # Clear any leaked cwd-based config lookup during init
    monkeypatch.delenv("SCENECRAFT_ROOT", raising=False)
    result = runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "myorg", "--admin", "admin1"])
    assert result.exit_code == 0, result.output
    # Pin subsequent invocations to this root
    monkeypatch.setenv("SCENECRAFT_ROOT", str(tmp_path / ".scenecraft"))
    return tmp_path


def test_init_command(tmp_path, runner, cli, monkeypatch):
    monkeypatch.delenv("SCENECRAFT_ROOT", raising=False)
    result = runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "acme", "--admin", "pat"])
    assert result.exit_code == 0, result.output
    assert "Initialized .scenecraft" in result.output
    assert (tmp_path / ".scenecraft" / "server.db").exists()


def test_init_double_fails(initialized, runner, cli):
    result = runner.invoke(cli, ["init", "--root", str(initialized)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_token_command(initialized, runner, cli):
    result = runner.invoke(cli, ["token", "--user", "admin1", "--raw"])
    assert result.exit_code == 0, result.output
    token = result.output.strip()
    assert len(token) > 20
    assert "." in token  # JWT has dots


def test_token_generates_login_url(initialized, runner, cli):
    # Pin host so the test isn't sensitive to the machine's LAN IP
    result = runner.invoke(cli, ["token", "--user", "admin1", "--host", "localhost:8890"])
    assert result.exit_code == 0, result.output
    assert "/auth/login?code=" in result.output
    assert "http://localhost:8890" in result.output


def test_token_host_flag(initialized, runner, cli):
    result = runner.invoke(cli, ["token", "--user", "admin1", "--host", "scenecraft.example.com:443", "--scheme", "https"])
    assert result.exit_code == 0, result.output
    assert "https://scenecraft.example.com:443/auth/login?code=" in result.output


def test_token_unregistered_fails(initialized, runner, cli):
    result = runner.invoke(cli, ["token", "--user", "nobody"])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_org_list(initialized, runner, cli):
    result = runner.invoke(cli, ["org", "list"])
    assert result.exit_code == 0, result.output
    assert "myorg" in result.output


def test_org_create_and_list(initialized, runner, cli):
    result = runner.invoke(cli, ["org", "create", "neworg"])
    assert result.exit_code == 0, result.output
    assert "Created org: neworg" in result.output

    result = runner.invoke(cli, ["org", "list"])
    assert "neworg" in result.output


def test_user_add_and_list(initialized, runner, cli, tmp_path):
    pubkey_file = tmp_path / "id_test.pub"
    pubkey_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 test@host")

    result = runner.invoke(cli, ["user", "add", "jane", "--pubkey", str(pubkey_file), "--org", "myorg"])
    assert result.exit_code == 0, result.output
    assert "Created user: jane" in result.output
    assert "Added to org: myorg" in result.output

    result = runner.invoke(cli, ["user", "list"])
    assert "jane" in result.output
    assert "admin1" in result.output


def test_org_members(initialized, runner, cli):
    result = runner.invoke(cli, ["org", "members", "myorg"])
    assert result.exit_code == 0, result.output
    assert "admin1" in result.output


def test_session_list_empty(initialized, runner, cli):
    result = runner.invoke(cli, ["session", "list"])
    assert result.exit_code == 0, result.output
    assert "No active sessions" in result.output


def test_session_prune(initialized, runner, cli):
    result = runner.invoke(cli, ["session", "prune"])
    assert result.exit_code == 0, result.output
    assert "Pruned 0" in result.output


# ── Explicit --root override ──────────────────────────────────────

def test_commands_accept_explicit_root_flag(tmp_path, runner, cli, monkeypatch):
    """Every top-level command that operates on a .scenecraft accepts --root.

    This lets users pin the target root without env vars or cwd-walking, and
    ensures CLI + server can agree on the same root even from unrelated dirs.
    """
    # Scrub env to prove --root works on its own
    monkeypatch.delenv("SCENECRAFT_ROOT", raising=False)

    # Init a specific root
    r = runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "myorg", "--admin", "admin1"])
    assert r.exit_code == 0, r.output

    # org list with --root
    r = runner.invoke(cli, ["org", "list", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "myorg" in r.output

    # token with --root
    r = runner.invoke(cli, ["token", "--user", "admin1", "--raw", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert len(r.output.strip()) > 20

    # user list with --root
    r = runner.invoke(cli, ["user", "list", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "admin1" in r.output

    # session list with --root
    r = runner.invoke(cli, ["session", "list", "--root", str(tmp_path)])
    assert r.exit_code == 0, r.output


def test_root_flag_accepts_scenecraft_subdir_directly(tmp_path, runner, cli, monkeypatch):
    """--root can point at the parent OR at the .scenecraft dir itself."""
    monkeypatch.delenv("SCENECRAFT_ROOT", raising=False)
    runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "myorg", "--admin", "admin1"])
    sc = str(tmp_path / ".scenecraft")
    r = runner.invoke(cli, ["org", "list", "--root", sc])
    assert r.exit_code == 0, r.output
    assert "myorg" in r.output


def test_root_flag_missing_dir_errors(tmp_path, runner, cli, monkeypatch):
    monkeypatch.delenv("SCENECRAFT_ROOT", raising=False)
    r = runner.invoke(cli, ["org", "list", "--root", str(tmp_path / "nonexistent")])
    assert r.exit_code == 1
    assert "no .scenecraft directory" in r.output.lower()
