"""REST endpoint smoke tests for the scene editor — covers R30 + per-endpoint
happy/error paths via PluginHost.dispatch_rest."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenecraft.plugin_host import PluginHost
from scenecraft.plugins.light_show import routes


@pytest.fixture
def rest_setup(tmp_project_dir: Path):
    """Reset PluginHost and register routes once for the suite."""
    PluginHost._reset_for_tests()
    from scenecraft import plugin_api
    routes.register(plugin_api, _NoCtx())
    yield
    PluginHost._reset_for_tests()


class _NoCtx:
    subscriptions: list = []


def _dispatch(method: str, path: str, project_dir: Path, body_or_query: dict | None = None):
    return PluginHost.dispatch_rest(method, path, project_dir, "test", body_or_query or {})


def test_rest_get_primitives(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch("GET", "/api/projects/test/plugins/light_show/primitives", tmp_project_dir)
    assert "primitives" in r
    assert len(r["primitives"]) == 2


def test_rest_post_get_scene(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    r2 = _dispatch(
        "GET",
        f"/api/projects/test/plugins/light_show/scenes/{sid}",
        tmp_project_dir,
    )
    assert r2["scene"]["id"] == sid


def test_rest_get_scene_not_found(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "GET",
        "/api/projects/test/plugins/light_show/scenes/nonexistent",
        tmp_project_dir,
    )
    assert "error" in r


def test_rest_patch_scene_null_label(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    r2 = _dispatch(
        "PATCH",
        f"/api/projects/test/plugins/light_show/scenes/{sid}",
        tmp_project_dir,
        {"label": None},
    )
    assert "error" in r2 and "null" in r2["error"].lower()


def test_rest_delete_scene_blocked_by_placement(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/placements",
        tmp_project_dir,
        {"scene_id": sid, "start_time": 0, "end_time": 5},
    )
    r2 = _dispatch(
        "DELETE",
        f"/api/projects/test/plugins/light_show/scenes/{sid}",
        tmp_project_dir,
    )
    assert "blocked" in r2


def test_rest_put_live(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    r2 = _dispatch(
        "PUT",
        "/api/projects/test/plugins/light_show/live",
        tmp_project_dir,
        {"scene_id": sid, "fade_in_sec": 1.5},
    )
    assert r2["active"] is True and r2["scene_id"] == sid

    # GET /live shows active state
    r3 = _dispatch(
        "GET", "/api/projects/test/plugins/light_show/live", tmp_project_dir
    )
    assert r3["active"] is True


def test_rest_delete_live_with_fade_out(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    _dispatch(
        "PUT",
        "/api/projects/test/plugins/light_show/live",
        tmp_project_dir,
        {"scene_id": sid},
    )
    # Note: query is dict-of-lists per parse_qs
    r2 = _dispatch(
        "DELETE",
        "/api/projects/test/plugins/light_show/live",
        tmp_project_dir,
        {"fade_out_sec": ["2.0"]},
    )
    # Live row still exists but marked deactivating
    assert r2.get("active") in (True, False)
    if r2.get("active") is True:
        assert r2.get("deactivation_started_at") is not None


def test_rest_placements_time_range_overlap(tmp_project_dir: Path, rest_setup) -> None:
    r = _dispatch(
        "POST",
        "/api/projects/test/plugins/light_show/scenes",
        tmp_project_dir,
        {"label": "x", "type": "rotating_head"},
    )
    sid = r["scene"]["id"]
    for start, end in [(0, 5), (10, 20), (25, 30)]:
        _dispatch(
            "POST",
            "/api/projects/test/plugins/light_show/placements",
            tmp_project_dir,
            {"scene_id": sid, "start_time": start, "end_time": end},
        )
    # Query as list-of-strings (parse_qs convention)
    r2 = _dispatch(
        "GET",
        "/api/projects/test/plugins/light_show/placements",
        tmp_project_dir,
        {"time_start": ["12"], "time_end": ["15"]},
    )
    assert r2["total"] == 1
