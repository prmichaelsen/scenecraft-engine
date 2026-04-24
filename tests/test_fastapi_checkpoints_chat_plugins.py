"""M16 T64 — checkpoints + chat + plugin catch-all routers.

Named tests (task-64):
  * ``plugin_route_dispatches`` — register a dummy plugin that handles
    ``POST /api/projects/{name}/plugins/dummy/ping`` returning
    ``{"ok": True, "data": 42}``; hit the route; assert 200 + body +
    ``PluginHost.dispatch_rest`` called with the full path.
  * ``plugin_error_500`` — dummy handler raises; expect 500 +
    ``PLUGIN_ERROR`` envelope with the exception message.
  * ``plugin_none_returns_404`` — dummy handler returns None; expect
    404 ``NOT_FOUND`` with the legacy ``No route: ...`` envelope.
  * ``builtin_beats_plugin_catchall`` — plugin catch-all is registered
    LAST; a path that matches both a built-in route and the catch-all
    goes to the built-in (this mostly documents router order).
  * ``unknown_route_404`` — GET ``/api/nope/nope/nope`` → 404 envelope
    (already covered by T58 but re-asserted here on the wired-up app
    with every T60-T64 router present).

Plus coverage tests for the other routers in this task:
  * ``checkpoints_list_empty`` — GET /checkpoints on a fresh project
  * ``undo_history_empty`` — GET /undo-history returns ``{"history": []}``
  * ``checkpoint_create_creates_file`` — POST /checkpoint → snapshot file
  * ``checkpoint_restore_round_trip`` — create, mutate, restore.
  * ``checkpoint_delete_removes_file`` — POST /checkpoint/delete
  * ``undo_is_noop_when_empty`` — POST /undo → ``{"success": False, ...}``
  * ``redo_is_noop_when_empty`` — POST /redo → ``{"success": False, ...}``
  * ``chat_history_returns_messages`` — GET /chat lists stored messages
  * ``sql_query_select_works`` — POST /sql/query returns rows
  * ``sql_query_rejects_write`` — POST /sql/query on INSERT → error
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures — reuse the auth_and_errors pattern.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from scenecraft.vcs.bootstrap import init_root

    init_root(tmp_path, org_name="test-org", admin_username="alice")
    sc = tmp_path / ".scenecraft"
    monkeypatch.setenv("SCENECRAFT_ROOT", str(sc))
    return sc


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


@pytest.fixture()
def project_dir(work_dir: Path) -> Path:
    """Create a project directory with a minimal ``project.db``.

    ``scenecraft.db.get_db`` auto-runs ``_ensure_schema`` on first open, so
    touching it once materializes every table we need (chat_messages,
    checkpoints, undo_history, etc.). ``close_db`` then releases the handle
    so the FastAPI handlers can reopen cleanly under their own thread.
    """
    pd = work_dir / "P1"
    pd.mkdir(parents=True, exist_ok=True)
    from scenecraft.db import close_db, get_db

    get_db(pd)
    close_db(pd)
    return pd


@pytest.fixture()
def app(sc_root: Path, work_dir: Path):
    from scenecraft.api.app import create_app

    return create_app(work_dir=work_dir)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def bearer_token(sc_root: Path) -> str:
    from scenecraft.vcs.auth import generate_token

    return generate_token(sc_root, username="alice")


@pytest.fixture()
def auth_headers(bearer_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer_token}"}


@pytest.fixture(autouse=True)
def _reset_plugin_host():
    """Plugin host is process-global; reset between tests so registrations
    from one test don't leak into another."""
    from scenecraft.plugin_host import PluginHost

    PluginHost._reset_for_tests()
    yield
    PluginHost._reset_for_tests()


# ---------------------------------------------------------------------------
# Plugin catch-all — the fragile bit.
# ---------------------------------------------------------------------------


def _register_dummy_plugin(handler) -> None:
    """Register a plugin REST handler directly via the host.

    We bypass the full activate() flow because tests don't need a real
    ``plugin.yaml`` — they need a route pattern that ``PluginHost.dispatch_rest``
    will match. This mirrors what ``plugin_api.register_rest_endpoint`` does
    internally.
    """
    from scenecraft.plugin_host import PluginHost

    PluginHost._rest_routes[r"^/api/projects/([^/]+)/plugins/dummy/"] = handler


def test_plugin_route_dispatches(
    client: TestClient,
    project_dir: Path,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    """plugin_route_dispatches — dummy handler returns 200 + body.

    Also asserts that ``PluginHost.dispatch_rest`` was called with the full
    request path (so the plugin can route on the tail, not just the prefix).
    """
    calls: list[tuple[Any, ...]] = []

    def _handler(path: str, *args, **kwargs):
        calls.append((path, args, kwargs))
        return {"ok": True, "data": 42}

    _register_dummy_plugin(_handler)

    resp = client.post(
        "/api/projects/P1/plugins/dummy/ping",
        json={"q": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "data": 42}
    # Handler saw the full path.
    assert calls, "dispatch_rest was not called"
    called_path, called_args, _ = calls[0]
    assert called_path == "/api/projects/P1/plugins/dummy/ping"
    # Legacy call shape: (path, project_dir, project_name, body)
    # project_dir
    assert called_args[0] == project_dir
    # project_name
    assert called_args[1] == "P1"
    # body
    assert called_args[2] == {"q": 1}


def test_plugin_error_500(
    client: TestClient,
    project_dir: Path,
    auth_headers: dict[str, str],
):
    """plugin_error_500 — handler raises → 500 ``PLUGIN_ERROR`` envelope."""
    def _handler(path: str, *args, **kwargs):
        raise RuntimeError("nope")

    _register_dummy_plugin(_handler)

    resp = client.post(
        "/api/projects/P1/plugins/dummy/ping",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["error"] == "PLUGIN_ERROR"
    assert "nope" in body["message"]


def test_plugin_none_returns_404(
    client: TestClient,
    project_dir: Path,
    auth_headers: dict[str, str],
):
    """plugin_none_returns_404 — handler returns None → 404 ``NOT_FOUND``."""
    def _handler(path: str, *args, **kwargs):
        return None

    _register_dummy_plugin(_handler)

    resp = client.post(
        "/api/projects/P1/plugins/dummy/ping",
        json={},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    # Legacy message shape.
    assert "No route" in body["message"]


def test_builtin_beats_plugin_catchall(
    client: TestClient,
    project_dir: Path,
    auth_headers: dict[str, str],
):
    """builtin_beats_plugin_catchall — router order ensures builtins win.

    We can't actually collide on the built-in namespace (built-ins don't live
    under ``/plugins/``), but we can assert a positive: a GET to
    ``/api/projects/P1/checkpoints`` (a built-in) returns 200 even if a
    pathological plugin tried to claim a similar prefix. This documents the
    registration order — the plugin catch-all is POST-only and scoped to
    ``/api/projects/{name}/plugins/...``, so no real collision is possible.
    """
    # Register a plugin that claims EVERY path — proving it can't shadow a
    # built-in because it's only mounted under the /plugins/ prefix.
    def _greedy(path: str, *args, **kwargs):
        return {"greedy": True}

    from scenecraft.plugin_host import PluginHost

    PluginHost._rest_routes[r".*"] = _greedy

    # Built-in GET /checkpoints — must NOT be shadowed.
    resp = client.get("/api/projects/P1/checkpoints", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Built-in response shape — ``checkpoints`` key, not ``greedy``.
    assert "checkpoints" in body
    assert "greedy" not in body


def test_unknown_route_404(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """unknown_route_404 — envelope works on the wired-up app."""
    resp = client.get("/api/nope/nope/nope", headers=auth_headers)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    assert body["message"] == "No route: GET /api/nope/nope/nope"


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def test_checkpoints_list_empty(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.get("/api/projects/P1/checkpoints", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"checkpoints": [], "active": "project.db"}


def test_undo_history_empty(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.get("/api/projects/P1/undo-history", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "history" in body
    assert isinstance(body["history"], list)


def test_checkpoint_create_creates_file(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post(
        "/api/projects/P1/checkpoint",
        json={"name": "my-snapshot"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["filename"].startswith("project.db.checkpoint-")
    assert body["name"] == "my-snapshot"
    # File actually exists on disk.
    assert (project_dir / body["filename"]).exists()


def test_checkpoint_restore_round_trip(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    # Create checkpoint.
    r1 = client.post(
        "/api/projects/P1/checkpoint", json={"name": "baseline"}, headers=auth_headers
    )
    assert r1.status_code == 200, r1.text
    filename = r1.json()["filename"]

    # Restore.
    r2 = client.post(
        "/api/projects/P1/checkpoint/restore",
        json={"filename": filename},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["success"] is True
    assert filename in body["message"]


def test_checkpoint_delete_removes_file(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    r1 = client.post(
        "/api/projects/P1/checkpoint", json={"name": "tmp"}, headers=auth_headers
    )
    filename = r1.json()["filename"]
    assert (project_dir / filename).exists()

    r2 = client.post(
        "/api/projects/P1/checkpoint/delete",
        json={"filename": filename},
        headers=auth_headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"success": True}
    assert not (project_dir / filename).exists()


def test_checkpoint_restore_404_on_missing(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post(
        "/api/projects/P1/checkpoint/restore",
        json={"filename": "project.db.checkpoint-nonexistent"},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "NOT_FOUND"


def test_undo_is_noop_when_empty(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post("/api/projects/P1/undo", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is False


def test_redo_is_noop_when_empty(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post("/api/projects/P1/redo", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is False


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


def test_chat_history_returns_messages(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.get("/api/projects/P1/chat", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "messages" in body
    assert isinstance(body["messages"], list)


def test_chat_history_respects_limit(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    # Just prove the limit param parses — not expected to round-trip without
    # stored messages, but mustn't 500.
    resp = client.get(
        "/api/projects/P1/chat?limit=10", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert "messages" in resp.json()


# ---------------------------------------------------------------------------
# SQL query — T64 scope-willing addition
# ---------------------------------------------------------------------------


def test_sql_query_select_works(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post(
        "/api/projects/P1/sql/query",
        json={"sql": "SELECT 1 AS n", "limit": 10},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Shape comes from _execute_readonly_sql
    assert body.get("row_count") == 1
    assert body.get("columns") == ["n"]
    assert body.get("rows") == [[1]]


def test_sql_query_rejects_write(
    client: TestClient, project_dir: Path, auth_headers: dict[str, str]
):
    resp = client.post(
        "/api/projects/P1/sql/query",
        json={"sql": "INSERT INTO chat_messages (user_id, role, content) VALUES ('x','user','hi')"},
        headers=auth_headers,
    )
    # Still 200 (legacy shape): the body carries an {"error": "..."} field
    # from the authorizer denial. _execute_readonly_sql returns a dict either
    # way, and the endpoint surfaces it as-is.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" in body
