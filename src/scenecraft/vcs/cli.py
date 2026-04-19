"""Top-level CLI commands for scenecraft — init, token, org/user/session management.

These commands are registered directly on the main scenecraft CLI group (not nested
under a 'vcs' subcommand) because authentication and session management are part of
the core server flow, not a version-control-only concern.
"""

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
from .auth import generate_token, create_login_code
from .sessions import list_sessions, prune_sessions


def _require_root() -> Path:
    # Prefer the VCS root derived from the configured projects_dir — this is
    # the same root the server uses, so CLI and server always agree.
    from scenecraft.config import get_projects_dir
    pd = get_projects_dir()
    if pd is not None:
        candidate = find_root(pd)
        if candidate is not None:
            return candidate
    root = find_root()
    if root is None:
        click.echo("Error: not inside a .scenecraft directory. Run 'scenecraft init' first.", err=True)
        raise SystemExit(1)
    return root


# ── init ─────────────────────────────────────────────────────────

@click.command("init")
@click.option("--org", default="default", help="Name of the initial org (default: 'default')")
@click.option("--admin", default=None, help="Admin username (default: current OS user)")
@click.option("--root", default=".", type=click.Path(), help="Root directory (default: cwd)")
def init_cmd(org: str, admin: str | None, root: str):
    """Initialize a new .scenecraft directory with an org and admin user."""
    try:
        sc = init_root(Path(root), org_name=org, admin_username=admin)
        click.echo(f"Initialized .scenecraft at {sc}")
        click.echo(f"  Org: {org}")
        click.echo(f"  Admin: {admin or 'current OS user'}")
    except FileExistsError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)


# ── token ────────────────────────────────────────────────────────

def _detect_primary_ip() -> str:
    """Best-effort detection of this machine's primary LAN/WAN IP."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


@click.command("token")
@click.option("--user", default=None, help="Username (default: current OS user)")
@click.option("--expiry", default=24, type=int, help="Token expiry in hours (default: 24)")
@click.option("--host", default=None, help="Host:port for the browser login URL (default: this machine's IP + :8890)")
@click.option("--scheme", default="http", type=click.Choice(["http", "https"]), help="URL scheme (default: http)")
@click.option("--open/--no-open", "open_browser", default=False, help="Attempt to open the URL in the local browser")
@click.option("--raw", is_flag=True, default=False, help="Print just the JWT (no URL) — for scripts")
def token_cmd(user: str | None, expiry: int, host: str | None, scheme: str, open_browser: bool, raw: bool):
    """Generate a login URL for authenticating your browser session.

    Default flow: generates a JWT, stores it against a one-time code, and prints
    a URL you can open to log in. The URL can only be used once and expires
    after 5 minutes.

    Use --raw to print just the JWT for scripting or manual Authorization headers.
    """
    if host is None:
        host = f"{_detect_primary_ip()}:8890"
    root = _require_root()
    try:
        tok = generate_token(root, username=user, expiry_hours=expiry)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    if raw:
        click.echo(tok)
        return

    code = create_login_code(root, tok)
    url = f"{scheme}://{host}/auth/login?code={code}"
    click.echo(url)
    click.echo("")
    click.echo(f"Valid for 5 minutes. Open in your browser (use SSH port-forward if {host.split(':')[0]} is remote).")

    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass


# ── org ──────────────────────────────────────────────────────────

@click.group("org")
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


# ── user ─────────────────────────────────────────────────────────

@click.group("user")
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


# ── session ──────────────────────────────────────────────────────

@click.group("session")
def session_group():
    """Manage editing sessions."""
    pass


@session_group.command("list")
def session_list():
    """List all active sessions."""
    root = _require_root()
    sessions = list_sessions(root)
    if not sessions:
        click.echo("No active sessions.")
        return
    for s in sessions:
        click.echo(f"  {s['id']}  {s['username']}  {s['org']}/{s['project']}  branch={s['branch']}  last_active={s['last_active']}")


@session_group.command("prune")
@click.option("--days", default=7, type=int, help="Remove sessions inactive for more than N days (default: 7)")
def session_prune(days: int):
    """Remove stale sessions and their working copy files."""
    root = _require_root()
    count = prune_sessions(root, max_age_days=days)
    click.echo(f"Pruned {count} stale session(s).")


# ── Registration helper (called from scenecraft.cli:main) ────────

def register_commands(main_group: click.Group) -> None:
    """Register all top-level server commands on the main scenecraft CLI."""
    main_group.add_command(init_cmd)
    main_group.add_command(token_cmd)
    main_group.add_command(org_group)
    main_group.add_command(user_group)
    main_group.add_command(session_group)
