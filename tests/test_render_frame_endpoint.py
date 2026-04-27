"""End-to-end tests for GET /api/projects/:name/render-frame (T65 TestClient)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import numpy as np
import pytest

from fastapi.testclient import TestClient
from scenecraft.api.app import create_app
from scenecraft.db import (
    _migrated_dbs, add_keyframe, add_transition, close_db, get_db, set_meta_bulk,
)


FPS = 24
WIDTH = 320
HEIGHT = 240


def _make_gradient_video(path: Path, seconds: float = 1.0) -> None:
    import cv2

    n_frames = int(seconds * FPS)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    for i in range(n_frames):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = (i * 11) % 256
        frame[:, :, 2] = (i * 13) % 256
        writer.write(frame)
    writer.release()


@pytest.fixture
def project_env():
    work_dir = Path(tempfile.mkdtemp())
    project_name = "renderframe_project"
    project_dir = work_dir / project_name
    project_dir.mkdir()

    get_db(project_dir)
    set_meta_bulk(project_dir, {
        "title": "renderframe",
        "fps": FPS,
        "resolution": [WIDTH, HEIGHT],
        "motion_prompt": "",
        "default_transition_prompt": "",
    })

    add_keyframe(project_dir, {
        "id": "kf_001", "timestamp": "0:00.00", "section": "",
        "source": "", "prompt": "start", "selected": 0, "candidates": [],
    })
    add_keyframe(project_dir, {
        "id": "kf_002", "timestamp": "0:01.00", "section": "",
        "source": "", "prompt": "end", "selected": 0, "candidates": [],
    })
    add_transition(project_dir, {
        "id": "tr_001", "from": "kf_001", "to": "kf_002",
        "duration_seconds": 1.0, "slots": 1, "action": "",
        "selected": [0], "remap": {"method": "linear", "target_duration": 0},
    })

    sel_dir = project_dir / "selected_transitions"
    sel_dir.mkdir(parents=True)
    _make_gradient_video(sel_dir / "tr_001_slot_0.mp4", seconds=1.0)

    app = create_app(work_dir=work_dir)
    client = TestClient(app, raise_server_exceptions=False)

    yield {
        "project_name": project_name,
        "project_dir": project_dir,
        "client": client,
    }

    close_db(project_dir)
    _migrated_dbs.discard(str(project_dir / "project.db"))
    shutil.rmtree(work_dir)


def _get(env, path: str):
    """GET helper returning TestClient response."""
    return env["client"].get(path)


def test_render_frame_returns_jpeg(project_env):
    name = project_env["project_name"]
    resp = _get(project_env, f"/api/projects/{name}/render-frame?t=0.25")
    assert resp.status_code == 200
    ct = resp.headers.get("Content-Type", "")
    assert "image/jpeg" in ct
    body = resp.content
    assert len(body) > 100
    # JPEG magic bytes
    assert body[:2] == b"\xff\xd8"
    assert body[-2:] == b"\xff\xd9"


def test_render_frame_honors_quality_param(project_env):
    name = project_env["project_name"]
    high = _get(project_env, f"/api/projects/{name}/render-frame?t=0.25&quality=95").content
    low = _get(project_env, f"/api/projects/{name}/render-frame?t=0.25&quality=30").content
    assert len(high) > len(low), "q=95 JPEG should be larger than q=30"


def test_render_frame_clamps_out_of_range_t(project_env):
    """Requesting t past the timeline should return the last frame, not an error."""
    name = project_env["project_name"]
    resp = _get(project_env, f"/api/projects/{name}/render-frame?t=99")
    assert resp.status_code == 200


def test_render_frame_rejects_bad_t(project_env):
    name = project_env["project_name"]
    resp = _get(project_env, f"/api/projects/{name}/render-frame?t=not-a-number")
    assert resp.status_code == 400


def test_render_frame_unknown_project_404(project_env):
    resp = _get(project_env, "/api/projects/does-not-exist/render-frame?t=0")
    assert resp.status_code == 404


def test_render_frame_is_cached(project_env):
    """Second request for the same (t, quality) hits the cache."""
    from scenecraft.render.frame_cache import global_cache
    global_cache.clear()

    name = project_env["project_name"]
    path = f"/api/projects/{name}/render-frame?t=0.25"
    first = _get(project_env, path)
    first_body = first.content
    assert first.headers.get("X-Scenecraft-Cache") == "MISS"

    second = _get(project_env, path)
    second_body = second.content
    assert second.headers.get("X-Scenecraft-Cache") == "HIT"
    # Identical bytes from cache
    assert first_body == second_body


def test_cache_invalidates_on_range_call(project_env):
    """Cache entries drop when invalidate_range hits the stored t."""
    from scenecraft.render.frame_cache import global_cache
    global_cache.clear()

    name = project_env["project_name"]
    path = f"/api/projects/{name}/render-frame?t=0.25"
    _get(project_env, path)
    assert _get(project_env, path).headers.get("X-Scenecraft-Cache") == "HIT"

    # Invalidate a range covering t=0.25
    dropped = global_cache.invalidate_range(project_env["project_dir"], 0.0, 1.0)
    assert dropped >= 1

    assert _get(project_env, path).headers.get("X-Scenecraft-Cache") == "MISS"


def test_render_frame_empty_project_returns_no_content(project_env):
    """A project with no transitions yet can't be rendered — return a clear 404."""
    work_dir = project_env["project_dir"].parent
    empty = work_dir / "empty_project"
    empty.mkdir()
    get_db(empty)
    set_meta_bulk(empty, {"title": "empty", "fps": FPS, "resolution": [WIDTH, HEIGHT]})

    resp = _get(project_env, "/api/projects/empty_project/render-frame?t=0")
    assert resp.status_code == 404
    close_db(empty)
    _migrated_dbs.discard(str(empty / "project.db"))
