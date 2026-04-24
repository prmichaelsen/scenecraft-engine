"""REST-level tests for the generate-music plugin.

Covers the four endpoints in routes.py plus the plugin-host dispatch
fan-out by HTTP method. Uses the same mock_musicful + project_dir
fixtures as test_generate_music.py via pytest's conftest-style
parameter resolution (we duplicate the fixtures inline here for
isolation — these tests don't share state with the run/polling suite).
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
import urllib.parse
from pathlib import Path

import pytest


# ── Reuse the mock Musicful server from test_generate_music ─────────────

from scenecraft.plugins.generate_music.tests.test_generate_music import (  # noqa: E402
    MockMusicfulState,
    _make_handler,
    _free_port,
    _audio_url,
    _wait_terminal,
)


@pytest.fixture
def mock_musicful(monkeypatch):
    state = MockMusicfulState()
    port = _free_port()
    handler = _make_handler(state)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    import scenecraft.plugin_api as pa
    monkeypatch.setitem(
        pa.SERVICE_REGISTRY, "musicful",
        (f"http://127.0.0.1:{port}", "MUSICFUL_API_KEY", "x-api-key"),
    )
    monkeypatch.setenv("MUSICFUL_API_KEY", "test-key")

    from scenecraft.plugins.generate_music import generate_music as gm
    monkeypatch.setattr(gm, "POLL_INTERVAL_SECONDS", 0.05)

    # Reset route-level caches so each test sees a clean slate.
    from scenecraft.plugins.generate_music import routes
    routes._reset_cache_for_tests()

    yield state, port

    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def project_dir(tmp_path):
    from scenecraft.db import get_db
    pd = tmp_path / "project"
    pd.mkdir()
    get_db(pd)
    return pd


@pytest.fixture
def server_root(tmp_path, monkeypatch):
    root = tmp_path / ".scenecraft"
    root.mkdir()
    monkeypatch.setenv("SCENECRAFT_ROOT", str(root))
    from scenecraft.vcs.bootstrap import get_server_db
    get_server_db(root)
    return root


# ── _handle_run ────────────────────────────────────────────────────────

def test_run_happy_path_returns_ids(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1", "t2"])
    state.tasks["t1"] = {
        "id": "t1", "title": "A", "style": "dark", "duration": 100,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }
    state.tasks["t2"] = {
        "id": "t2", "title": "A2", "style": "dark", "duration": 110,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }

    from scenecraft.plugins.generate_music.routes import _handle_run
    result = _handle_run(
        "/api/projects/p1/plugins/generate-music/run",
        project_dir, "p1",
        {"action": "auto", "style": "dark cinematic"},
    )
    assert "error" not in result, result
    assert "generation_id" in result
    assert result["task_ids"] == ["t1", "t2"]
    assert "job_id" in result


def test_run_rejects_missing_style(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.routes import _handle_run
    result = _handle_run(
        "/api/projects/p1/plugins/generate-music/run",
        project_dir, "p1",
        {"action": "auto"},
    )
    assert "error" in result
    assert "style" in result["error"].lower()


def test_run_rejects_blank_style(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.routes import _handle_run
    result = _handle_run(
        "/api/projects/p1/plugins/generate-music/run",
        project_dir, "p1",
        {"action": "auto", "style": "   "},
    )
    assert "error" in result
    assert "style" in result["error"].lower()


def test_run_passes_entity_context_to_impl(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "A", "style": "warm", "duration": 60,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }

    from scenecraft.plugins.generate_music.routes import _handle_run
    result = _handle_run(
        "/x", project_dir, "p1",
        {
            "action": "auto", "style": "warm pads",
            "entity_type": "transition", "entity_id": "tr_A",
        },
    )
    assert "error" not in result, result
    gen = _wait_terminal(project_dir, result["generation_id"])
    assert gen["entity_type"] == "transition"
    assert gen["entity_id"] == "tr_A"


# ── _handle_list ───────────────────────────────────────────────────────

def test_list_unfiltered_returns_all(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.extend([["t1"], ["t2"]])
    state.tasks["t1"] = {
        "id": "t1", "title": "A", "style": "dark", "duration": 60,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }
    state.tasks["t2"] = {
        "id": "t2", "title": "B", "style": "light", "duration": 60,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }

    from scenecraft.plugins.generate_music.routes import _handle_run, _handle_list
    r1 = _handle_run("/x", project_dir, "p1", {"action": "auto", "style": "dark"})
    r2 = _handle_run("/x", project_dir, "p1", {"action": "auto", "style": "light"})
    _wait_terminal(project_dir, r1["generation_id"])
    _wait_terminal(project_dir, r2["generation_id"])

    result = _handle_list("/x", project_dir, "p1", {})
    ids = {g["id"] for g in result["generations"]}
    assert {r1["generation_id"], r2["generation_id"]} <= ids


def test_list_filtered_by_entity(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.extend([["t1"], ["t2"]])
    for tid in ("t1", "t2"):
        state.tasks[tid] = {
            "id": tid, "title": tid, "style": "dark", "duration": 60,
            "audio_url": _audio_url(port), "cover_url": "", "status": 3,
        }

    from scenecraft.plugins.generate_music.routes import _handle_run, _handle_list
    r_a = _handle_run(
        "/x", project_dir, "p1",
        {"action": "auto", "style": "a", "entity_type": "audio_clip", "entity_id": "clip_A"},
    )
    r_b = _handle_run(
        "/x", project_dir, "p1",
        {"action": "auto", "style": "b", "entity_type": "audio_clip", "entity_id": "clip_B"},
    )
    _wait_terminal(project_dir, r_a["generation_id"])
    _wait_terminal(project_dir, r_b["generation_id"])

    result = _handle_list(
        "/x", project_dir, "p1",
        {"entityType": "audio_clip", "entityId": "clip_A"},
    )
    ids = {g["id"] for g in result["generations"]}
    assert r_a["generation_id"] in ids
    assert r_b["generation_id"] not in ids


# ── _handle_retry ──────────────────────────────────────────────────────

def test_retry_creates_new_row_with_reused_from(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    # First run fails (no tasks came back).
    state.generate_responses.append([])
    from scenecraft.plugins.generate_music.routes import _handle_run, _handle_retry
    first = _handle_run(
        "/x", project_dir, "p1", {"action": "auto", "style": "doomed run"}
    )
    assert "error" in first  # all-empty generate → error + failed row

    from scenecraft import plugin_api
    failed = plugin_api.get_music_generations_for_entity(project_dir, limit=10)
    assert len(failed) >= 1
    failed_id = failed[0]["id"]
    assert failed[0]["status"] == "failed"

    # Second run succeeds.
    state.generate_responses.append(["t-retry"])
    state.tasks["t-retry"] = {
        "id": "t-retry", "title": "retry", "style": "doomed run", "duration": 60,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }
    retry_result = _handle_retry(
        f"/api/projects/p1/plugins/generate-music/generations/{failed_id}/retry",
        project_dir, "p1", {},
    )
    assert "error" not in retry_result, retry_result
    new_id = retry_result["generation_id"]
    assert new_id != failed_id

    new_row = plugin_api.get_music_generation(project_dir, new_id)
    assert new_row["reused_from"] == failed_id

    # Original row untouched.
    orig = plugin_api.get_music_generation(project_dir, failed_id)
    assert orig["status"] == "failed"


def test_retry_refuses_nonfailed(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "ok", "style": "a", "duration": 60,
        "audio_url": _audio_url(port), "cover_url": "", "status": 3,
    }
    from scenecraft.plugins.generate_music.routes import _handle_run, _handle_retry
    r = _handle_run("/x", project_dir, "p1", {"action": "auto", "style": "a"})
    gen_id = r["generation_id"]
    _wait_terminal(project_dir, gen_id)

    retry_result = _handle_retry(
        f"/api/projects/p1/plugins/generate-music/generations/{gen_id}/retry",
        project_dir, "p1", {},
    )
    assert "error" in retry_result
    assert "only failed" in retry_result["error"].lower()


def test_retry_404_unknown_id(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.routes import _handle_retry
    result = _handle_retry(
        "/api/projects/p1/plugins/generate-music/generations/nope/retry",
        project_dir, "p1", {},
    )
    assert "error" in result
    assert "not found" in result["error"].lower()


# ── _handle_credits + TTL cache ────────────────────────────────────────

def test_credits_returns_key_info(mock_musicful, project_dir, server_root):
    state, _ = mock_musicful
    from scenecraft.plugins.generate_music.routes import _handle_credits
    result = _handle_credits("/x", project_dir, "p1", {})
    assert result["credits"] == 237
    assert "last_checked_at" in result


def test_credits_cached_within_ttl(mock_musicful, project_dir, server_root):
    state, _ = mock_musicful
    from scenecraft.plugins.generate_music.routes import _handle_credits

    # Two rapid GETs → only one upstream call.
    before = state.key_info_call_count if hasattr(state, "key_info_call_count") else None
    _handle_credits("/x", project_dir, "p1", {})
    _handle_credits("/x", project_dir, "p1", {})

    # Mock doesn't count get_api_key_info calls; use a side-channel: mutate
    # the stored credits and confirm second read is stale.
    state.key_info = {"key_music_counts": 999}
    r = _handle_credits("/x", project_dir, "p1", {})
    assert r["credits"] == 237  # stale cached value, upstream change not seen


def test_credits_force_refresh_busts_cache(mock_musicful, project_dir, server_root):
    state, _ = mock_musicful
    from scenecraft.plugins.generate_music.routes import _handle_credits

    _handle_credits("/x", project_dir, "p1", {})
    state.key_info = {"key_music_counts": 42}

    r = _handle_credits("/x", project_dir, "p1", {"refresh": "1"})
    assert r["credits"] == 42


def test_credits_missing_api_key_returns_error(monkeypatch, project_dir, server_root):
    monkeypatch.delenv("MUSICFUL_API_KEY", raising=False)
    from scenecraft.plugins.generate_music import routes
    routes._reset_cache_for_tests()
    result = routes._handle_credits("/x", project_dir, "p1", {})
    assert result["credits"] is None
    assert "error" in result


# ── Plugin-host dispatch by method ─────────────────────────────────────

def test_dispatch_routes_post_and_get_to_different_handlers(mock_musicful, project_dir, server_root):
    from scenecraft.plugin_host import PluginHost
    from scenecraft import plugin_api
    from scenecraft.plugins.generate_music import routes

    PluginHost._reset_for_tests()

    # Use a minimal context-shaped stub — register() only reads subscriptions.
    class _Ctx:
        subscriptions: list = []
    ctx = _Ctx()

    routes.register(plugin_api, ctx)

    # POST /run → _handle_run (validation: returns `error` for missing style)
    post_result = PluginHost.dispatch_rest(
        "POST",
        "/api/projects/p1/plugins/generate-music/run",
        project_dir, "p1", {},
    )
    assert post_result is not None
    assert "error" in post_result

    # GET /credits → _handle_credits
    get_result = PluginHost.dispatch_rest(
        "GET",
        "/api/projects/p1/plugins/generate-music/credits",
        project_dir, "p1", {},
    )
    assert get_result is not None
    assert "credits" in get_result

    # Cleanup.
    for d in ctx.subscriptions:
        d.dispose()


def test_dispatch_wrong_method_returns_none(mock_musicful, project_dir, server_root):
    from scenecraft.plugin_host import PluginHost
    from scenecraft import plugin_api
    from scenecraft.plugins.generate_music import routes

    PluginHost._reset_for_tests()

    class _Ctx:
        subscriptions: list = []
    ctx = _Ctx()
    routes.register(plugin_api, ctx)

    # GET on a POST-only endpoint → no handler fires.
    result = PluginHost.dispatch_rest(
        "GET",
        "/api/projects/p1/plugins/generate-music/run",
        project_dir, "p1", {},
    )
    assert result is None

    for d in ctx.subscriptions:
        d.dispose()
