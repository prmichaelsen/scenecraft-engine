"""M16 T60 — projects + misc routers.

Named tests from task-60 step 5:

    - get_route_parity        (projects-slice GET routes register + auth-gate)
    - post_route_parity       (meta/config slice POSTs register + round-trip)
    - deprecated_noops_preserved  (4× /version/* legacy shapes)
    - extra_fields_ignored    (R10 permissiveness on 3 representative POSTs)
    - no_body_post_works      (narrative POST with empty body is accepted)

TDD order: authored before src/scenecraft/api/routers/projects.py,
workspace.py, settings.py, ingredients.py, bench.py, markers.py,
prompt_roster.py, config.py exist. Red phase = every POST/GET 404s or
401s against the T57/T58/T59 scaffold.

Why this suite exists (vs. the "capture fixture against legacy" approach
in the task plan): the legacy server requires a live process, a
.scenecraft root, and a populated work_dir to capture fixtures. For the
40 routes in this task we assert **structural** parity — path shape,
method, operation_id, auth gate, envelope — and round-trip **behavior**
parity on a handful where the DB call is trivial. Byte-level body
parity is covered by the full-migration crawl test in T65.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A .scenecraft root with one registered user — enables auth."""
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
    """Create a bare project P1 under work_dir and ensure its DB exists."""
    p = work_dir / "P1"
    p.mkdir(parents=True, exist_ok=True)
    # Initialize the SQLite schema so db.get_meta / db.get_markers / … don't
    # crash on a fresh tmp dir. get_db is idempotent.
    from scenecraft.db import get_db

    get_db(p)
    return p


@pytest.fixture()
def app(sc_root: Path, work_dir: Path) -> FastAPI:
    from scenecraft.api.app import create_app

    return create_app(work_dir=work_dir)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def bearer_token(sc_root: Path) -> str:
    from scenecraft.vcs.auth import generate_token

    return generate_token(sc_root, username="alice")


@pytest.fixture()
def auth_headers(bearer_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer_token}"}


# ---------------------------------------------------------------------------
# GET parity — every GET route in this task is registered and auth-gated
# ---------------------------------------------------------------------------


# (path_template, after_substituting_{name}_as_"P1", expects_200_after_auth)
GET_ROUTES = [
    "/api/projects",
    "/api/browse",
    "/api/projects/P1/ls",
    "/api/projects/P1/bin",
    "/api/projects/P1/keyframes",
    "/api/projects/P1/beats",
    "/api/projects/P1/narrative",
    "/api/projects/P1/watched-folders",
    "/api/projects/P1/workspace-views",
    "/api/projects/P1/settings",
    "/api/projects/P1/section-settings",
    "/api/projects/P1/ingredients",
    "/api/projects/P1/bench",
    "/api/projects/P1/markers",
    "/api/projects/P1/prompt-roster",
    # /branches lives under the VCS tree (.scenecraft/orgs/...) — its 200
    # path requires the project to exist in an org. Tested separately below.
    "/api/projects/P1/version/history",
    "/api/projects/P1/version/diff",
    "/api/config",
]


@pytest.mark.parametrize("path", GET_ROUTES + ["/api/projects/P1/branches"])
def test_get_route_parity_auth_required(client: TestClient, path: str):
    """Every GET route must 401 without auth (not 404 — proves registered)."""
    resp = client.get(path)
    assert resp.status_code == 401, f"{path} returned {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error"] == "UNAUTHORIZED"


def test_branches_route_registered_but_project_not_in_vcs_tree(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """``/branches`` is VCS-tree-scoped — the .scenecraft tree test-fixture
    has no project P1 registered under an org, so legacy + new both 404.
    What matters: the route is registered and auth is enforced.
    """
    resp = client.get("/api/projects/P1/branches", headers=auth_headers)
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    assert "project" in body["message"].lower()


@pytest.mark.parametrize("path", GET_ROUTES)
def test_get_route_parity_authed_returns_200(
    client: TestClient, auth_headers: dict[str, str], path: str, project_dir: Path
):
    """Every GET route returns 200 when authed + project exists."""
    resp = client.get(path, headers=auth_headers)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# POST parity — happy-path routes register and round-trip
# ---------------------------------------------------------------------------


def test_post_route_parity_create_project(
    client: TestClient, auth_headers: dict[str, str], work_dir: Path
):
    """POST /api/projects/create — creates project dir + returns success."""
    resp = client.post(
        "/api/projects/create",
        json={"name": "brand_new_project"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["name"] == "brand_new_project"
    assert (work_dir / "brand_new_project").is_dir()


def test_post_route_parity_create_project_missing_name(
    client: TestClient, auth_headers: dict[str, str]
):
    """POST /api/projects/create with body {} — 400 BAD_REQUEST about name."""
    resp = client.post("/api/projects/create", json={}, headers=auth_headers)
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    assert "name" in body["message"].lower()


def test_post_route_parity_update_meta(
    client: TestClient,
    auth_headers: dict[str, str],
    project_dir: Path,
):
    """POST /api/projects/P1/update-meta writes meta + returns success."""
    resp = client.post(
        "/api/projects/P1/update-meta",
        json={"motion_prompt": "dreamy"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["meta"].get("motion_prompt") == "dreamy"


def test_post_route_parity_markers_add_update_remove(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """Marker CRUD — add then update then remove; each returns 200 success."""
    r_add = client.post(
        "/api/projects/P1/markers/add",
        json={"id": "m1", "time": 10.0, "label": "beat drop", "type": "note"},
        headers=auth_headers,
    )
    assert r_add.status_code == 200, r_add.text
    assert r_add.json() == {"success": True, "id": "m1"}

    r_update = client.post(
        "/api/projects/P1/markers/update",
        json={"id": "m1", "label": "chorus"},
        headers=auth_headers,
    )
    assert r_update.status_code == 200, r_update.text
    assert r_update.json() == {"success": True}

    r_remove = client.post(
        "/api/projects/P1/markers/remove",
        json={"id": "m1"},
        headers=auth_headers,
    )
    assert r_remove.status_code == 200, r_remove.text
    assert r_remove.json() == {"success": True}


def test_post_route_parity_update_config(
    client: TestClient, auth_headers: dict[str, str]
):
    """POST /api/config with body — returns {"success": True}."""
    resp = client.post("/api/config", json={}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}


def test_post_route_parity_settings_roundtrip(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """POST /api/projects/P1/settings persists allow-listed fields + echoes them."""
    resp = client.post(
        "/api/projects/P1/settings",
        json={"preview_quality": 72},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["preview_quality"] == 72


# ---------------------------------------------------------------------------
# Deprecated /version/* noops — status + body shape frozen
# ---------------------------------------------------------------------------


def test_deprecated_noops_preserved_version_commit(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """POST /api/projects/P1/version/commit — legacy noop shape."""
    resp = client.post(
        "/api/projects/P1/version/commit", json={}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "noChanges": True}


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects/P1/version/checkout",
        "/api/projects/P1/version/branch",
        "/api/projects/P1/version/delete-branch",
    ],
)
def test_deprecated_noops_preserved_version_others(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path, path: str
):
    """The other three /version/* routes return 410 GONE with the legacy message."""
    resp = client.post(path, json={}, headers=auth_headers)
    assert resp.status_code == 410, f"{path}: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["error"] == "GONE"
    assert "git versioning removed" in body["message"].lower()


def test_deprecated_noops_preserved_version_history_diff(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """GET /version/history + /version/diff — 200 with legacy empty shapes."""
    r_hist = client.get(
        "/api/projects/P1/version/history", headers=auth_headers
    )
    assert r_hist.status_code == 200
    assert r_hist.json() == {"commits": [], "branch": "", "branches": []}

    r_diff = client.get("/api/projects/P1/version/diff", headers=auth_headers)
    assert r_diff.status_code == 200
    assert r_diff.json() == {"changes": []}


# ---------------------------------------------------------------------------
# extra-fields-ignored — Pydantic models use extra="ignore" (R10)
# ---------------------------------------------------------------------------


def test_extra_fields_ignored_create_project(
    client: TestClient, auth_headers: dict[str, str], work_dir: Path
):
    resp = client.post(
        "/api/projects/create",
        json={"name": "p_extra", "unknown_field": 42, "another": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert (work_dir / "p_extra").is_dir()


def test_extra_fields_ignored_update_meta(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    resp = client.post(
        "/api/projects/P1/update-meta",
        json={"motion_prompt": "x", "completely_bogus": [1, 2, 3]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


def test_extra_fields_ignored_markers_add(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    resp = client.post(
        "/api/projects/P1/markers/add",
        json={"id": "mx", "time": 1.0, "label": "x", "garbage": "ignored"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True, "id": "mx"}


# ---------------------------------------------------------------------------
# no-body-post-works — POST with no body on a route where legacy permits it
# ---------------------------------------------------------------------------


def test_no_body_post_works_narrative(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    """POST /api/projects/P1/narrative with {} (omitted sections) — 200 success."""
    resp = client.post(
        "/api/projects/P1/narrative", json={}, headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert "sections" in body  # legacy returns sections count


# ---------------------------------------------------------------------------
# Workspace views round-trip — upsert then fetch then delete
# ---------------------------------------------------------------------------


def test_workspace_views_upsert_fetch_delete(
    client: TestClient, auth_headers: dict[str, str], project_dir: Path
):
    # Upsert
    r_set = client.post(
        "/api/projects/P1/workspace-views/default",
        json={"layout": {"panels": ["timeline", "chat"]}},
        headers=auth_headers,
    )
    assert r_set.status_code == 200, r_set.text
    assert r_set.json() == {"success": True}

    # Get one
    r_get = client.get(
        "/api/projects/P1/workspace-views/default", headers=auth_headers
    )
    assert r_get.status_code == 200, r_get.text
    assert r_get.json() == {"layout": {"panels": ["timeline", "chat"]}}

    # List all
    r_list = client.get(
        "/api/projects/P1/workspace-views", headers=auth_headers
    )
    assert r_list.status_code == 200
    assert "default" in r_list.json()["views"]

    # Delete
    r_del = client.post(
        "/api/projects/P1/workspace-views/default/delete",
        json={},
        headers=auth_headers,
    )
    assert r_del.status_code == 200
    assert r_del.json() == {"success": True}


# ---------------------------------------------------------------------------
# operation_id — every route registered by T60 has an operation_id set
# ---------------------------------------------------------------------------


# Expected operation_ids for each (method, path_template) in this task.
# Load-bearing for T66 tool-codegen; if this list diverges from the
# routers, the codegen spec's test in T66 will fail.
T60_OPERATION_IDS = {
    ("GET", "/api/projects"): "list_projects",
    ("POST", "/api/projects/create"): "create_project",
    ("GET", "/api/browse"): "browse_projects",
    ("GET", "/api/projects/{name}/ls"): "list_project_files",
    ("GET", "/api/projects/{name}/bin"): "get_project_bin",
    ("GET", "/api/projects/{name}/keyframes"): "get_keyframes",
    ("GET", "/api/projects/{name}/beats"): "get_project_beats",
    ("GET", "/api/projects/{name}/narrative"): "get_narrative",
    ("POST", "/api/projects/{name}/narrative"): "update_narrative",
    ("POST", "/api/projects/{name}/update-meta"): "update_meta",
    ("POST", "/api/projects/{name}/import"): "import_project",
    ("POST", "/api/projects/{name}/save-as-still"): "save_as_still",
    ("POST", "/api/projects/{name}/extend-video"): "extend_video",
    ("GET", "/api/projects/{name}/watched-folders"): "get_watched_folders",
    ("POST", "/api/projects/{name}/watch-folder"): "watch_folder",
    ("POST", "/api/projects/{name}/unwatch-folder"): "unwatch_folder",
    ("GET", "/api/projects/{name}/branches"): "list_branches",
    ("POST", "/api/projects/{name}/branches"): "create_branch",
    ("POST", "/api/projects/{name}/branches/delete"): "delete_branch",
    ("POST", "/api/projects/{name}/checkout"): "checkout_branch",
    ("GET", "/api/projects/{name}/version/history"): "version_history_deprecated",
    ("GET", "/api/projects/{name}/version/diff"): "version_diff_deprecated",
    ("POST", "/api/projects/{name}/version/commit"): "version_commit_noop",
    ("POST", "/api/projects/{name}/version/checkout"): "version_checkout_noop",
    ("POST", "/api/projects/{name}/version/branch"): "version_branch_noop",
    ("POST", "/api/projects/{name}/version/delete-branch"): "version_delete_branch_noop",
    ("GET", "/api/projects/{name}/workspace-views"): "list_workspace_views",
    ("GET", "/api/projects/{name}/workspace-views/{view_name}"): "get_workspace_view",
    ("POST", "/api/projects/{name}/workspace-views/{view_name}"): "upsert_workspace_view",
    ("POST", "/api/projects/{name}/workspace-views/{view_name}/delete"): "delete_workspace_view",
    ("GET", "/api/projects/{name}/settings"): "get_settings",
    ("POST", "/api/projects/{name}/settings"): "update_settings",
    ("GET", "/api/projects/{name}/section-settings"): "get_section_settings",
    ("POST", "/api/projects/{name}/section-settings"): "update_section_settings",
    ("GET", "/api/projects/{name}/ingredients"): "list_ingredients",
    ("POST", "/api/projects/{name}/ingredients/promote"): "promote_ingredient",
    ("POST", "/api/projects/{name}/ingredients/remove"): "remove_ingredient",
    ("POST", "/api/projects/{name}/ingredients/update"): "update_ingredient",
    ("GET", "/api/projects/{name}/bench"): "get_bench",
    ("POST", "/api/projects/{name}/bench/capture"): "bench_capture",
    ("POST", "/api/projects/{name}/bench/upload"): "bench_upload",
    ("POST", "/api/projects/{name}/bench/add"): "bench_add",
    ("POST", "/api/projects/{name}/bench/remove"): "bench_remove",
    ("GET", "/api/projects/{name}/markers"): "list_markers",
    ("POST", "/api/projects/{name}/markers/add"): "add_marker",
    ("POST", "/api/projects/{name}/markers/update"): "update_marker",
    ("POST", "/api/projects/{name}/markers/remove"): "remove_marker",
    ("GET", "/api/projects/{name}/prompt-roster"): "get_prompt_roster",
    ("POST", "/api/projects/{name}/prompt-roster/add"): "add_prompt_roster_entry",
    ("POST", "/api/projects/{name}/prompt-roster/update"): "update_prompt_roster_entry",
    ("POST", "/api/projects/{name}/prompt-roster/remove"): "remove_prompt_roster_entry",
    ("POST", "/api/config"): "update_config",
}


def test_every_t60_route_has_expected_operation_id(app: FastAPI):
    """Every (method, path) in T60 has a route with the expected operation_id.

    Load-bearing for T66 tool-codegen: renaming any operation_id breaks the
    generated ``chat_tools.py`` downstream.
    """
    # Build a lookup: {(method, path): operation_id}
    registered: dict[tuple[str, str], str] = {}
    for route in app.routes:
        if not hasattr(route, "methods") or not hasattr(route, "path"):
            continue
        for method in route.methods:
            if method == "HEAD":
                continue
            op_id = getattr(route, "operation_id", None) or getattr(
                route, "name", None
            )
            registered[(method, route.path)] = op_id

    missing = []
    mismatched = []
    for (method, path), expected_id in T60_OPERATION_IDS.items():
        actual = registered.get((method, path))
        if actual is None:
            missing.append(f"{method} {path}")
        elif actual != expected_id:
            mismatched.append(f"{method} {path}: {actual!r} != {expected_id!r}")

    assert not missing, f"Missing routes: {missing}"
    assert not mismatched, f"Mismatched operation_ids: {mismatched}"
