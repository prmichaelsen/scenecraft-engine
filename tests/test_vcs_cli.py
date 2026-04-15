"""Tests for scenecraft VCS CLI commands."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from scenecraft.vcs.cli import vcs_group


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def initialized(tmp_path, runner):
    """Run init and return the temp root."""
    result = runner.invoke(vcs_group, ["init", "--root", str(tmp_path), "--org", "myorg", "--admin", "admin1"])
    assert result.exit_code == 0, result.output
    # CLI uses find_root() which walks from cwd, but our functions take explicit paths
    # So we test the underlying functions via CLI invocations with --root
    return tmp_path


def test_init_command(tmp_path, runner):
    result = runner.invoke(vcs_group, ["init", "--root", str(tmp_path), "--org", "acme", "--admin", "pat"])
    assert result.exit_code == 0
    assert "Initialized .scenecraft" in result.output
    assert (tmp_path / ".scenecraft" / "server.db").exists()


def test_init_double_fails(initialized, runner):
    result = runner.invoke(vcs_group, ["init", "--root", str(initialized)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_token_command(initialized, runner, monkeypatch):
    # token command needs find_root() to work — monkeypatch cwd
    monkeypatch.chdir(initialized)
    result = runner.invoke(vcs_group, ["token", "--user", "admin1"])
    assert result.exit_code == 0
    token = result.output.strip()
    assert len(token) > 20
    assert "." in token  # JWT has dots


def test_token_unregistered_fails(initialized, runner, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(vcs_group, ["token", "--user", "nobody"])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_org_list(initialized, runner, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(vcs_group, ["org", "list"])
    assert result.exit_code == 0
    assert "myorg" in result.output


def test_org_create_and_list(initialized, runner, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(vcs_group, ["org", "create", "neworg"])
    assert result.exit_code == 0
    assert "Created org: neworg" in result.output

    result = runner.invoke(vcs_group, ["org", "list"])
    assert "neworg" in result.output


def test_user_add_and_list(initialized, runner, monkeypatch, tmp_path):
    monkeypatch.chdir(initialized)

    # Create a fake pubkey file
    pubkey_file = tmp_path / "id_test.pub"
    pubkey_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 test@host")

    result = runner.invoke(vcs_group, ["user", "add", "jane", "--pubkey", str(pubkey_file), "--org", "myorg"])
    assert result.exit_code == 0
    assert "Created user: jane" in result.output
    assert "Added to org: myorg" in result.output

    result = runner.invoke(vcs_group, ["user", "list"])
    assert "jane" in result.output
    assert "admin1" in result.output


def test_org_members(initialized, runner, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(vcs_group, ["org", "members", "myorg"])
    assert result.exit_code == 0
    assert "admin1" in result.output
