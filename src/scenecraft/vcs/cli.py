"""CLI commands for scenecraft VCS — init, org, user management."""

from __future__ import annotations

from pathlib import Path

import click

from .bootstrap import (
    init_root,
    find_root,
    create_org,
    create_user,
    add_user_to_org,
    list_orgs,
    list_users,
    list_org_members,
)
from .auth import generate_token


def _require_root() -> Path:
    root = find_root()
    if root is None:
        click.echo("Error: not inside a .scenecraft directory. Run 'scenecraft init' first.", err=True)
        raise SystemExit(1)
    return root


@click.group("vcs")
def vcs_group():
    """Version control commands — init, org, user management."""
    pass


@vcs_group.command()
@click.option("--org", default="default", help="Name of the initial org (default: 'default')")
@click.option("--admin", default=None, help="Admin username (default: current OS user)")
@click.option("--root", default=".", type=click.Path(), help="Root directory (default: cwd)")
def init(org: str, admin: str | None, root: str):
    """Initialize a new .scenecraft directory with an org and admin user."""
    try:
        sc = init_root(Path(root), org_name=org, admin_username=admin)
        click.echo(f"Initialized .scenecraft at {sc}")
        click.echo(f"  Org: {org}")
        click.echo(f"  Admin: {admin or 'current OS user'}")
    except FileExistsError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


@vcs_group.command()
@click.option("--user", default=None, help="Username (default: current OS user)")
@click.option("--expiry", default=24, type=int, help="Token expiry in hours (default: 24)")
def token(user: str | None, expiry: int):
    """Generate a JWT authentication token for the current user."""
    root = _require_root()
    try:
        tok = generate_token(root, username=user, expiry_hours=expiry)
        click.echo(tok)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


# ── Org commands ─────────────────────────────────────────────────

@vcs_group.group("org")
def org_group():
    """Manage organizations."""
    pass


@org_group.command("create")
@click.argument("name")
def org_create(name: str):
    """Create a new organization."""
    root = _require_root()
    create_org(root, name)
    click.echo(f"Created org: {name}")


@org_group.command("list")
def org_list():
    """List all organizations."""
    root = _require_root()
    orgs = list_orgs(root)
    if not orgs:
        click.echo("No organizations found.")
        return
    for o in orgs:
        click.echo(f"  {o['name']}  (created: {o['created_at']})")


@org_group.command("members")
@click.argument("name")
def org_members(name: str):
    """List members of an organization."""
    root = _require_root()
    members = list_org_members(root, name)
    if not members:
        click.echo(f"No members in org '{name}'.")
        return
    for m in members:
        click.echo(f"  {m['username']}  role={m['role']}  joined={m['joined_at']}")


# ── User commands ────────────────────────────────────────────────

@vcs_group.group("user")
def user_group():
    """Manage users."""
    pass


@user_group.command("add")
@click.argument("username")
@click.option("--pubkey", default=None, type=click.Path(exists=True), help="Path to SSH public key file")
@click.option("--role", default="editor", type=click.Choice(["admin", "editor", "viewer"]))
@click.option("--org", default=None, help="Add user to this org after creation")
def user_add(username: str, pubkey: str | None, role: str, org: str | None):
    """Register a new user."""
    root = _require_root()
    pubkey_content = ""
    if pubkey:
        pubkey_content = Path(pubkey).read_text().strip()
    create_user(root, username, pubkey=pubkey_content, role=role)
    click.echo(f"Created user: {username}  role={role}")
    if org:
        add_user_to_org(root, org, username)
        click.echo(f"  Added to org: {org}")


@user_group.command("list")
def user_list():
    """List all registered users."""
    root = _require_root()
    users = list_users(root)
    if not users:
        click.echo("No users found.")
        return
    for u in users:
        click.echo(f"  {u['username']}  role={u['role']}  created={u['created_at']}")
