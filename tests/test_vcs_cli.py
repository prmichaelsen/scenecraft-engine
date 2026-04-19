"""Tests for scenecraft top-level CLI commands (init, token, org, user, session)."""

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
def initialized(tmp_path, runner, cli):
    """Run init and return the temp root."""
    result = runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "myorg", "--admin", "admin1"])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_init_command(tmp_path, runner, cli):
    result = runner.invoke(cli, ["init", "--root", str(tmp_path), "--org", "acme", "--admin", "pat"])
    assert result.exit_code == 0
    assert "Initialized .scenecraft" in result.output
    assert (tmp_path / ".scenecraft" / "server.db").exists()


def test_init_double_fails(initialized, runner, cli):
    result = runner.invoke(cli, ["init", "--root", str(initialized)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_token_command(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["token", "--user", "admin1", "--raw"])
    assert result.exit_code == 0
    token = result.output.strip()
    assert len(token) > 20
    assert "." in token  # JWT has dots


def test_token_generates_login_url(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["token", "--user", "admin1"])
    assert result.exit_code == 0
    assert "/auth/login?code=" in result.output
    assert "http://localhost:8890" in result.output


def test_token_host_flag(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["token", "--user", "admin1", "--host", "scenecraft.example.com:443", "--scheme", "https"])
    assert result.exit_code == 0
    assert "https://scenecraft.example.com:443/auth/login?code=" in result.output


def test_token_unregistered_fails(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["token", "--user", "nobody"])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_org_list(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["org", "list"])
    assert result.exit_code == 0
    assert "myorg" in result.output


def test_org_create_and_list(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["org", "create", "neworg"])
    assert result.exit_code == 0
    assert "Created org: neworg" in result.output

    result = runner.invoke(cli, ["org", "list"])
    assert "neworg" in result.output


def test_user_add_and_list(initialized, runner, cli, monkeypatch, tmp_path):
    monkeypatch.chdir(initialized)

    pubkey_file = tmp_path / "id_test.pub"
    pubkey_file.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 test@host")

    result = runner.invoke(cli, ["user", "add", "jane", "--pubkey", str(pubkey_file), "--org", "myorg"])
    assert result.exit_code == 0
    assert "Created user: jane" in result.output
    assert "Added to org: myorg" in result.output

    result = runner.invoke(cli, ["user", "list"])
    assert "jane" in result.output
    assert "admin1" in result.output


def test_org_members(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["org", "members", "myorg"])
    assert result.exit_code == 0
    assert "admin1" in result.output


def test_session_list_empty(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["session", "list"])
    assert result.exit_code == 0
    assert "No active sessions" in result.output


def test_session_prune(initialized, runner, cli, monkeypatch):
    monkeypatch.chdir(initialized)
    result = runner.invoke(cli, ["session", "prune"])
    assert result.exit_code == 0
    assert "Pruned 0" in result.output
